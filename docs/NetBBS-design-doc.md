# NetBBS architecture and product design

This document is the **current normative design** for NetBBS. It describes what
the system means, what users and node operators may rely on, and the boundaries
future implementations must preserve.

It is not a chronological decision diary. The former numbered sign-off rounds,
superseded alternatives, corrections, and intermediate implementation status
remain available through Git history. Do not reconstruct that chronology here.

Use project sources in this order:

1. this document for product, protocol, authority, and long-lived UX decisions;
2. current GitHub issues for unresolved work and acceptance criteria;
3. `docs/NetBBS-worklog.md` for durable implementation constraints and lessons;
4. source, migrations, tests, and Git history for exact implementation detail.

When these sources disagree, investigate and update the stale source. Do not
choose whichever answer is most convenient.

This is a developer reference. Start with the
[developer handbook](NetBBS-Developer-Handbook.md) for practical entry points.
The [SysOp handbook](NetBBS-SysOp-Handbook.md) covers installation and operation;
the [user handbook](NetBBS-User-Handbook.md) covers everyday use. Neither requires
reading this design reference first.

## Current status

- Phases 1 and 2 are complete as working standalone BBS software.
- The post-Phase-2 local additions—Communities, identity attestation,
  asynchronous personal mail, and self-update foundations—are substantially
  implemented.
- Phase 3 connectivity and asynchronous services are implemented: signed
  identity, discovery, persistent events/peers, linked boards/channels/file
  catalogues, mail, catch-up, and relays.
- Phase 4 trust, reputation, quarantine, and recovery controls are implemented.
  NetBBS Link remains **private and experimental federation** pending issue
  #131's human/operational checks and sustained dogfood (#83). Independent
  implementation compatibility remains unclaimed (#71).
- Phase 5 has authenticated live chat, presence, scrollback-on-join, live
  private messages, and one-/two-relay paths. Cross-node `/dm` invitations and
  simultaneous background channel memberships are not implemented.
- Phase 7 has native, DOSBox-X, VM (qemu) and remote door adapters, three bundled games,
  companion services, and door API 3 with optional outbound board posting.
  Compatibility is bounded to the documented host/game profiles.
- Advanced Link governance, Link Communities, and the remaining deferred
  protocol features are future work; current issues track their scope.

Implementation status belongs beside the relevant design rule below and must be
updated in place. Do not append victory narratives or test-count snapshots.

---

## 1. Product identity, terminology, and principles

### 1.1 Names

- **NetBBS** is the software project.
- **NetBBS Link** is the decentralized network connecting NetBBS nodes. “The
  Link” is acceptable informal shorthand.
- **Board** always means a message board.
- **Area** always means a file area. Never call a file area a board.
- **Link** prefixes features which exist specifically because of NetBBS Link:
  **Link message**, **Link Community**, **Link-wide** presence or chat.
- An ordinary local resource which participates in NetBBS Link keeps its normal
  noun with the adjective **linked**: linked board, linked channel, linked file
  area. Do not rename such resources into “Link boards” or similar proper
  nouns.

### 1.2 Foundational principles

NetBBS Link is foundational, not an add-on. Every durable local feature should
be designed with its possible network extension in mind, even when the local
version ships first.

The standalone BBS must remain complete and useful without NetBBS Link. Local
packages must not import Phase-3 federation code merely to perform ordinary
local work.

Node sovereignty is non-negotiable:

- no master node exists;
- no node, moderator, or majority vote can force another operator to store,
  display, or delete content;
- carrying remote content is always a local decision;
- moderation and trust signals may propagate, but enforcement remains local;
- remote closure or suppression events cannot remotely erase bytes already
  stored by another node.

The design prefers correctness, explicit authority, bounded resource use, and
visible failure over cleverness or silent degradation.

### 1.3 Non-goals

NetBBS is not intended to:

- recreate a centralized social network behind a terminal interface;
- promise anonymity from a user’s own home-node operator for ordinary
  password-only accounts;
- make every local feature network-wide immediately;
- replicate every file byte to every node;
- hide unresolved trust decisions behind signature verification alone;
- preserve historical BBS protocol constraints when they conflict with the
  native NetBBS model.

---

## 2. Platform, architecture, and scale

### 2.1 Target platform and stack

**Platform support tiers (issue #81).** Without an explicit contract,
portability becomes accidental — a NetBSD-specific detail creeping into
core code, or Linux/macOS breakage going unnoticed, or (the opposite
failure) primary-target constraints getting quietly weakened to
accommodate a platform that was never meant to drive architecture.
Four tiers, in order:

- **Tier 1 / primary — NetBSD.** Every design and dependency choice must
  work here; this is the platform "does this work?" defaults to. NetBBS
  itself is obtained and updated only from its official GitHub releases
  or tagged source; shipping NetBBS through pkgsrc or another external
  package manager is neither planned nor supported. pkgsrc remains the
  preferred source for external dependencies on NetBSD. Existing
  Tier-1-driven decisions include PyNaCl/libsodium for core identity and
  FTS5 availability confirmed by tracing
  pkgsrc's actual `lang/python312` → `databases/sqlite3` build chain
  (worklog §6), not assumed.
- **Tier 2 / supported — mainstream Linux distributions**, using
  ordinary Python packaging (venv/pip) and system service managers
  (systemd). A regression here is a real bug — *unless* fixing it would
  weaken a Tier-1 constraint, in which case Tier-1 wins; Tier 2 never
  gets to relax what Tier 1 needs.
- **Tier 3 / best-effort — other POSIX systems** (macOS, FreeBSD,
  illumos, etc.), until a maintainer or user demonstrates a build/run
  exercised with some regularity. No dedicated design effort beyond
  staying ordinary POSIX-portable Python.
- **Development-only compatibility — Windows.** Convenient for local
  development and testing (this project's own dev sandbox routinely
  runs here) — never a target for production semantics that depend on
  POSIX facilities: real signal delivery (`asyncio.loop.
  add_signal_handler`), `os.kill(pid, 0)`-style liveness probes, POSIX
  file permission bits, `termios`/`tty` raw-mode terminal control.
  Existing platform branches — `netbbs.net.local_terminal` (raw-mode
  input), `netbbs.backup._process_is_running` (restore's liveness
  probe), `netbbs.__main__`'s signal-handler setup — already draw
  exactly this line, each with its own `sys.platform`/`os.name` check
  and a comment naming Windows as the dev/test fallback, never the
  deployment target; this tier list makes that existing practice an
  explicit policy rather than an implicit one three separate modules
  happened to agree on.

Consequences of the tier list, not separate rules:

- A new dependency is evaluated against the Tier-1 target *before*
  adoption: is it available through pkgsrc, does it need an unusual
  compiler toolchain, or does it assume behavior unlikely to hold on
  NetBSD? We make a strong effort to choose dependencies available in
  pkgsrc so a NetBSD deployment remains straightforward. Tier-2/3
  convenience is never sufficient justification by itself.
- Platform-specific code stays isolated in a small number of narrow
  modules/functions (the three named above), never scattered
  `sys.platform`/`os.name` checks through domain code. Core/domain/
  protocol modules (`netbbs.boards`, `netbbs.link`, etc.) are plain
  POSIX-portable Python with no platform branching at all.
- Installation/service examples (issue #82) use GitHub-hosted NetBBS
  releases on every platform and cover Tier 1 (NetBSD/rc.d) and Tier 2
  (Linux/systemd) explicitly; a Tier-3 or
  development-only path is documented as such, never presented as an
  equally-supported option.

- Runtime: Python 3.11+ with asyncio.
- Storage: one SQLite database per node, using WAL mode.
- Core cryptography: PyNaCl/libsodium. The optional SSH transport uses
  AsyncSSH and therefore `cryptography`; its source build on NetBSD needs
  Rust, a C compiler and Python headers, OpenSSL and libffi headers, and
  build-discovery tooling. Keep that toolchain isolated to the optional
  feature and avoid adding comparable build burdens without compelling
  benefit.
- User transports: Telnet, SSH, and web/xterm.js.
- Asynchronous Link transport: signed HTTP+JSON.
- Real-time Link chat transport: Noise Protocol Framework.

### 2.2 Modular boundaries

The system is a modular package, not a monolithic script.

- `netbbs.auth` owns accounts and authentication.
- `netbbs.identity` owns cryptographic identities and addressing.
- `netbbs.boards`, `netbbs.files`, `netbbs.chat`, and `netbbs.mail` own local
  domain state.
- `netbbs.communities` owns Community state and inherited-value resolution.
- `netbbs.moderation` owns shared authorization and audit primitives.
- `netbbs.rendering` owns ANSI, reflow, screen-buffer, and editor-independent
  rendering behavior.
- `netbbs.net` owns user-facing flows, sessions, and transport orchestration.
- `netbbs.link` owns Link events, protocol, transport, persistence, discovery,
  synchronization, relaying, and local-to-Link bridges.
- `netbbs.storage` owns migrations, database connections, and execution lanes.

Domain functions are normally synchronous and `db`-first. Async session or
network code dispatches blocking work through a `DatabaseLane`. Link bridge
modules may depend on local domains; local domains must remain Link-unaware.

Rendering, protocol, storage, and transport concerns remain distinct. A generic
transport must not learn every event’s product semantics, and domain storage
must not decide how terminal output looks.

### 2.3 Expected scale

The primary deployment remains one modest self-hosted node operated by one
SysOp. The architecture targets:

- dozens to low hundreds of concurrent interactive sessions;
- small-to-medium Link deployments initially;
- correctness across multiple nodes even before large deployments exist.

SQLite is appropriate at this scale. The first expected scaling pressure is
write contention and queued background work, not raw interactive connection
count. Scale decisions beyond demonstrated workloads must be based on measured
behavior using the deterministic multi-node harness, not estimates alone.

---

## 3. User connectivity, rendering, and interaction

### 3.1 Connection methods

Telnet, SSH, and web/xterm.js are first-class user transports. Product behavior
should be transport-independent unless a capability genuinely requires a byte
stream, browser code, or another transport-specific primitive.

A key reaches the server when it is typed on every transport, including a
phone's on-screen keyboard in the browser (issue #1066). An Android keyboard
with prediction composes each word, and xterm.js sends a composition only
when it ends, so letter hotkeys used to wait for Enter. The browser terminal
asks the keyboard not to predict, correct or capitalise, and on Android sends
the composed word as it changes: the letters added, and a DEL for each one
taken away. What the keyboard turns the word into as it ends (an
autocorrection) is not sent, since the letters are already on the server and
a hotkey must not answer twice. Desktop browsers are left as they were: a
desktop input method composes a spelling that is then converted (romaji,
pinyin), and only the converted text is meant to be sent.

The browser terminal draws box-drawing and block characters so they join
between rows, as a classic terminal does (issue #1083). Rows are exactly one
font height tall (`lineHeight` 1), and xterm.js's WebGL renderer draws those
characters itself rather than taking them from the font, so a frame's
vertical lines and block art stay unbroken whatever font the browser picked.
Without WebGL (an old browser, a blocked GPU, a lost context) xterm.js keeps
its DOM renderer, where the line height of 1 keeps most fonts' own glyphs
touching.

### 3.2 Rendering model

Use hybrid terminal rendering:

- ordinary screens use ANSI/VT100 text with reflow;
- every terminal-facing prose line and interactive prompt is wrapped to the
  session's negotiated display-column width before the terminal can soft-wrap
  or clip it; wrap boundaries never leave a trailing or leading blank, and
  screen-specific truncation is reserved for deliberately single-line chrome;
- cursor-addressed screen-buffer rendering is reserved for interfaces which
  benefit from it, such as fullscreen editors and pinned chat rows;
- the minimum supported terminal is 40x24;
- screens must degrade clearly rather than corrupting output when a terminal is
  too small or lacks a capability.

Trusted SysOp-authored ANSI art preserves each authored row when it fits because
unconditionally reflowing it would corrupt its layout. It is not exempt from the
width bound: an over-width row still wraps so no content disappears beyond the
right edge. Cursor positioning is preserved while a bounded cursor model tracks
absolute, relative, save/restore, and bare-carriage-return column changes;
numeric parameters are clamped without per-column expansion, and tabs are
normalized before measuring. Ordinary product copy, including text surrounding
an art preview, uses normal prose wrapping. Prompts reserve two columns for a
possible East Asian Wide/Fullwidth first input character. CLI errors measure the
stderr terminal they are written to, rather than assuming stdout has the same
width.

Untrusted user text is sanitized before styling. Trusted ANSI is added only
after sanitization. Nested colored fragments are composed independently because
an SGR reset does not restore an outer color.

**Character set per session (issue #929).** Every session has one output
character set: UTF-8, CP437 or ASCII. Screens are composed in Unicode as
before. `Session.write`, the one path all text output already takes, maps the
composed text to the session's character set before the transport encodes it.
No screen has to know which set a caller has. The mapping is in three layers:

- NetBBS's own glyphs go through one curated table (`rendering/charset`).
  Characters CP437 has pass through unchanged, including the single and double
  box-drawing characters, the block and shade characters, and `·` and `»`.
  The rest get deliberate substitutes: rounded corners become square ones,
  `›` becomes `»`, and stars and diamonds become `*`.
- Text a caller wrote is mapped only on output. Stored text stays Unicode, so
  readers on UTF-8 lose nothing. Each character becomes, in order of
  preference: its exact CP437 character, its base letter after the accents are
  removed (NFKD), an entry in a small table for letters and punctuation that
  do not decompose (`ł`, `ø`, typographic quotes), or `?`.
- A substitute always has the display width of the character it replaces. A
  wide character becomes two cells, zero-width marks are dropped, and control
  characters and escape sequences are never mapped. Width is still measured
  on the Unicode text (§3.2's wrapping, §3.6's columns), so no layout moves
  when a caller's set changes. That makes `…` a single `.` in CP437 and ASCII,
  which reads as a full stop, so a screen that cuts text picks its marker
  with `ellipsis_for(session)`: `…` on a UTF-8 terminal, `...` elsewhere.
- A box-drawing character the table does not list is worked out from the
  directions its Unicode name says it draws: CP437 gets the light line of
  that shape (it has no heavy, dashed or mixed light/heavy lines), and ASCII
  gets `+`, `|`, `-` or `=`. That covers CP437's own mixed single/double
  corners and tees too, so a CP437 door or CP437 art shown on an ASCII
  session keeps its frames.

ASCII is true 7-bit: nothing above 0x7F reaches an ASCII session, including
accented letters in posts, which become their base letters. CP437 is one byte
per character in both directions: typed input is decoded as CP437 rather than
UTF-8, and Telnet doubles the 0xFF byte (CP437's non-breaking space) as RFC 854
requires. Raw byte paths keep their own rules. A Zmodem transfer is binary and
bypasses the mapping. A door that speaks CP437 reaches a CP437 session
unchanged; any other combination is transcoded in `DoorTerminal`, in both
directions. SysOp ANSI art is decoded to Unicode when it is loaded and mapped
like any other text, so art authored in CP437 reaches a CP437 terminal byte for
byte ("SysOp art: storage and SAUCE" below covers what the art path adds).

**How the set is chosen.** A caller's preference is Auto (the default), Unicode,
CP437 or ASCII, and an explicit choice always wins over detection. Auto means:

- Telnet asks for the terminal type (TTYPE, RFC 1091) before the first byte
  of the welcome screen, waiting at most one second for the answer or the
  refusal. Keystrokes typed during the wait are kept. `syncterm`, `ansi-bbs`
  and `ansi` mean CP437; SyncTERM reports `syncterm` in its normal screen modes
  and some servers force `ansi-bbs`. Modern names (`xterm*`, `vt*`, `linux`,
  `screen*`, `tmux*`, `putty*` and similar) mean UTF-8. A client that refuses
  TTYPE, does not answer, or reports a name on neither list gets ASCII,
  everything included: NetBBS's own chrome and the SysOp's banner.
- SSH uses the terminal type from the PTY request, with the same lists; SyncTERM
  sends `syncterm` over SSH too. The pre-authentication banner goes out before
  any channel exists, so it is always ASCII. Until authentication completes the
  server sends no SSH_MSG_IGNORE padding (asyncssh would otherwise put one in
  front of every packet): Cryptlib, which SyncTERM 1.9 uses, gives up after
  more than three no-op packets in a row counting the one that ends them, and
  IGNORE, BANNER, IGNORE, FAILURE was four (issue #964). The padding protects
  CBC ciphers only, and none is offered.
- The browser terminal is always UTF-8.

Each Telnet and SSH connection logs one INFO line with the terminal types the
client reported, how the exchange ended (answered, refused, no answer, or the
SSH PTY request) and the character set chosen, so a SysOp can read what a
client calls itself. Reported names are kept to printable ASCII and 40
characters before they are stored or logged.

**Keys from classic terminals (issue #964).** SyncTERM sends BBS-convention
sequences for some editing keys (CTerm manual, "Sequences sent by SyncTERM"):
`ESC[K` for End, `ESC[V` and `ESC[U` for Page Up and Page Down, `ESC[@` for
Insert. Its Backspace sends 0x08 and its Delete 0x7F, while PuTTY and xterm
send 0x7F for Backspace. All of these are read as SyncTERM's keys only when the
first recognised terminal type is `syncterm` or `ansi-bbs`. Everywhere else
0x7F stays Backspace, and a bare `ESC[K` or `ESC[@` is pasted screen output
(erase in line, insert character), discarded as before. Only input decoding
changes: the same bytes sent by NetBBS are still screen commands.

After login, a caller whose set was not settled -- an unknown terminal, or one
that reported only `ansi` -- is asked once which of two sample lines looks
right: the same frame sent as UTF-8 and as CP437, or neither, which means
ASCII. The answer becomes their preference. Profile changes it later. The
earlier yes/no "Does that look garbled?" question and its Unicode/ASCII style
preference are replaced by this; an account that had switched to ASCII keeps
ASCII, and every other account moves to Auto. Screens that still vary their
decoration by style use the decorated variant unless the caller's preference is
ASCII; a CP437 session gets it mapped. An undetected terminal gets ASCII only
until the question after login settles it.

**Terminals that wrap at once (issue #964).** SyncTERM, like DOS ANSI.SYS,
moves to the next line the moment it writes a character in the last column;
xterm and its descendants wait for the next character. On the first kind, a
row exactly as wide as the screen followed by CR LF leaves a blank line, and
writing the bottom-right cell scrolls the screen. NetBBS avoids both without
any screen knowing about it:

- `Session.terminal_width`, the width every screen lays out in, is one column
  less than the width the terminal reported (`Session.physical_width`) when the
  session `wraps_immediately`. No generated row reaches the last column, so no
  row double-spaces and nothing writes the bottom-right cell. Classic BBS
  software did the same by designing for 79 columns.
- Which terminals: only one whose type is recognised as a modern UTF-8 emulator
  (the list above) is trusted to wait. A CP437 name, `ansi`, an unknown name or
  no answer at all counts as wrapping at once, and so does a caller who chose
  CP437, since only classic terminals read it. The cost of a wrong guess on a
  modern terminal is one unused column; the cost the other way is a
  double-spaced, scrolling screen.
- Art keeps the full width. SysOp banners and mastheads go through
  `write_preformatted_line`, which wraps at `physical_width`, so 80-column art
  keeps all 80 columns. On a terminal that wraps at once, a row that fills the
  width is sent without its CR LF, because the terminal has already moved to
  the next line. Banner files also lose the plain spaces at the end of each
  row when they are loaded (`trim_row_ends`): the art editor saves every row
  of its 80-column canvas in full, so a 60-column banner arrived as rows of
  exactly 80. Spaces painted by a background colour or reverse video stay.
- An art post (issue #711) is art too: its body lays out at `physical_width`
  (`post_body_width`) in the reader, the version history, the review screen and
  the moderation queue, so an 80-column drawing keeps its last column. Those
  screens write each row through `write_laid_out_row`, which sends a row wider
  than `terminal_width` -- only art can be -- the way `write_preformatted_line`
  does, and every other row as an ordinary line.
- Doors, the break-in screen copy and the web terminal's door resize use
  `physical_width`: a door is told the terminal's real size and draws for it
  itself. The screen copy also wraps where the caller's terminal does: on one
  that wraps at once it moves to the next line on the last column, and scrolls
  at the bottom-right cell, so the SysOp's snoop view and a break-in repaint
  match what the caller actually sees. The ANSI art editor's canvas stays 80
  columns wide; its status line on the bottom row is cut to `terminal_width`,
  so it never writes the last cell.

**Colour depth from the terminal type (issue #986).** A session is truecolor
when the client says so through `COLORTERM` (`truecolor` or `24bit`, over
Telnet NEW-ENVIRON or the SSH environment) and 256-colour otherwise. Classic
terminals send no `COLORTERM`, so the terminal type also counts: a client
whose first recognised name is `syncterm` gets truecolor
(`terminal_detect.terminal_supports_truecolor`). CTerm, SyncTERM's emulator,
handles SGR `38;2;R;G;B` and `48;2;R;G;B` -- the semicolon form NetBBS sends --
through an internal palette large enough for every cell of a 132x60 screen
(CTerm manual, `src/conio/cterm.adoc` at tag `syncterm-1.9`). An explicit
`COLORTERM` still decides when a client sends one, and `ansi-bbs` and `ansi`
stay at 256 colours because those names also cover clients with less. Doors,
banners, presets and gradients all read the session's depth through
`effective_truecolor`, so nothing per screen changes, and a caller's own
**Profile** colour-depth choice still overrides it after sign-in.

**SysOp art: storage and SAUCE (issue #929).** A banner or masthead is the
`.ans` file on disk, exactly as uploaded or saved; there is no second copy in
the database, so a SysOp editing the file over SFTP changes what callers see.
Everything below happens each time the file is read:

- A SAUCE record ([spec](https://www.acid.org/info/sauce/sauce.htm)) is parsed
  and removed before display: the 128-byte record, its optional comment block
  (`COMNT` plus 64 bytes per line) and the EOF byte (0x1A) that precedes them.
  Without this, scene art showed its title, author and group as junk under the
  picture. A file without SAUCE is read as before: UTF-8 if it decodes as
  UTF-8, CP437 otherwise. A SAUCE record marks the file as classic ANSI art,
  so it is always read as CP437.
- CP437 art uses the pictographs of the control range (☺ ♥ ♫ ► ⌂ and the
  rest). On the art path only, bytes 0x01–0x1F other than BEL, BS, TAB, LF,
  CR, EOF and ESC, and 0x7F, are those glyphs, not control characters. A CP437
  session gets the original byte back, a UTF-8 session the Unicode glyph, and
  an ASCII session a plain substitute. Elsewhere those bytes stay controls.
- iCE colours: classic art uses the blink attribute to mean a bright
  background. Art that sets blink together with a background colour is shown
  with the bright background and no blink, for every session, whether or not
  SAUCE sets the iCE flag. CTerm (SyncTERM) only shows bright backgrounds
  (100-107) with DECSET mode 33 on, and keeps them as the blink attribute,
  which blinks until mode 35 is on too. A CP437 session gets both modes before
  every art that needs them and never their reset: switching them off after
  the art made the cells already drawn blink (issue #1083).
- SAUCE width (TInfo1): art wider than the caller's `physical_width` is not
  drawn -- the screen falls back to what it shows without art (the default
  welcome banner, no masthead) instead of wrapping every row.
- SAUCE font (TInfoS): "IBM VGA" and its 437 variants are CP437. Any other
  font or code page is still decoded as CP437, and the SysOp console's banner
  screen warns that the art was made for another font.
- Title, author and group appear in the SysOp console's banner status and
  preview. The welcome banner can also carry a caller-facing credit line under
  the art, "art: Title by Author/Group" with missing parts left out; a SysOp
  toggle, off by default.
- The ANSI art editor writes a SAUCE record when it saves: width, lines, font
  "IBM VGA", the iCE flag, and any title, author and group the loaded file had.

**Art with live slots (issue #929, being built).** A SysOp can draw menu art
that NetBBS fills in per caller. Tokens drawn in the art mark where: `{menu
WxH}` for the caller's live item list, `{user N}`, `{mail N}` and the other
fields for live values, and `{prompt}` for the prompt. A token's top-left cell
is its position, its size is written in the token, and the colour it is drawn
in is the style of what fills it. Counts and levels (`{mail}`, `{online}`,
`{level}`, `{count}`) are bare numbers and the art supplies the words, and a
value is cut to its field, without an ellipsis when the field is too narrow
for one plus two characters (issue #1083). Tokens are plain ASCII so they survive
CP437, UTF-8 and every art editor. Art only decorates: the items are the ones
the caller may use, computed as for the generated menu, so a token can never
show an item a caller cannot use or hide one they can. When the caller's
items do not fit the region, that draw uses the generated menu instead and the
node logs it once; the console's check warns when a level-255 SysOp's menu
would not fit. ASCII callers, a terminal smaller than the art, and art that
fails the check get the generated menu too. Art narrower than the screen is
drawn left-aligned, and the prompt goes below the art unless a `{prompt}`
token places it. The main menu, the welcome and logoff banners and the three
lists below use slots, and the main menu also takes hand-drawn items. Pacing
works for the welcome banner, the main menu's art and the three lists' art.

*List screens.* The Boards, file areas and Chat channels lists take a `{list
WxH}` region. The current page fills it, one row per entry: the number to
press for it, the name, and one compact column the screen chooses (unread
posts for boards, files for areas, people for channels). A board, area or
channel whose name requirement the caller does not meet shows "needs
verification" in that column instead, so the gate note §3.6 requires survives
in art too. Descriptions and full tables appear only on the generated list. A
page holds as many entries as the region has rows; numbering, one-digit
selection, browser clicks, search and the paging keys work as on the generated
list, and the navigation block and prompt go below the art unless `{prompt}`
places it. The cursor row is drawn reversed in the token's colour. Three more
fields serve lists: `{title N}`, `{page N}` ("2/5") and `{count N}`. A region
under 3 rows, or one that leaves the name under 12 columns, falls back to the
generated list, as does every case that makes the main menu fall back.

*Hand-drawn items.* A SysOp may draw the menu's items into the art instead of
leaving them to a `{menu WxH}` region (written `{menu}` below; it is the same
token). Every bracketed key drawn in the art,
`[K]`, marks one item; the item spans the run of text around it, bounded by
two or more spaces or by a box-drawing or block character (U+2500-259F), which
is also how the browser terminal finds what a click means. So a frame drawn
one space from an item is not part of it, and `[M]essage boards│ [N]ew scan`
is two items, as a panel gutter between them should make it. A key drawn as
such a character, `[─]`, is still a key. An item drawn inside a slot is not an
item, since the slot is drawn over it. A run holding two keys with neither
between them, such as `[B]oards [E]-mail` with one space, is one item holding
both: both count as drawn, it is blanked only for a caller who can use
neither, and the console's check suggests two spaces or a frame character
between them. An item the caller cannot use is
blanked: its cells are repainted as spaces in the background each cell shows,
so frames and fills stay whole and the caller sees only what the generated
menu would show. Items the caller can use that the art does not draw go into
the art's `{menu}` region; art with no `{menu}` region and an undrawn item
falls back to the generated menu, so nothing is ever hidden, and art that
draws its items needs no `{menu}` region of its own. A drawn `[X]` that is no
main-menu key is the SysOp's decoration: it stays as drawn, and the console's
check lists it. `[S]` means the SysOp console to a SysOp and the staff console
to a staff member, so a drawn `[S]ysOp` item is blanked for staff, whose
`[S]taff` item goes into `{menu}`, and the other way round. Buttons drawn over
several rows are not supported.

*Pacing.* Art can be played at an emulated line speed so that it draws itself
the way it did over a modem (`netbbs.net.art_pacing`). Each banner's speed is
off, 2400, 9600 or 38400 bps, off by default, set with **Speed** on its console
screen. NetBBS paces the bytes itself, in small chunks, and checks for a
waiting key between them (`Session.take_waiting_key`); any key ends the effect
and writes the rest at once, and it is consumed with anything typed behind it,
so Enter cannot submit an empty prompt that follows. One draw is paced for at
most 5 seconds; past that the rest goes out at once. Each piece of art plays
once per connection: the welcome banner when a caller connects, the main
menu's art on the first main menu, and a list's art (slot art or the
masthead above the generated list) on the first visit to that list in a
session, each list with its own speed (`pick_item`'s `art_speed` and
`art_once`). It never plays on a page change, a cursor move, a search, a
redraw, after a notice, on the screen
restored after a break-in, or in a door. Plain banners play in the order they
were drawn, so cursor-moving ANSI animations work, and art that moves the
cursor back over rows it drew is exempt from the row-end trimming of still art
(`revisits_rows`), since its trailing spaces may erase an earlier frame. Slot
art is rebuilt cell by cell and is revealed top to bottom; the whole art is
prepared once (iCE colours, CTerm's bright backgrounds) so no chunk loses a
colour state. Nothing is paced for a session with no live terminal
(`Session.paces_art`, set only by the Telnet, SSH and web transports), during
a break-in, for ASCII callers, or for callers who chose quick under
**Profile → [Q]uick or animated banners** (animated by default). The welcome
banner plays before sign-in, when the caller's choice is not known yet, so it
always follows the SysOp's speed (issue #1055).

A SysOp may override three of the node's branding colors -- accent (board/
channel/user names and other navigable-item branding), header (section
titles and frame borders), and clock (the main-menu prompt's time display)
-- independently (issue #162, part three of the skinning initiative the
welcome banner/main-menu masthead already started). `netbbs.net.node_theme`
resolves each node-wide RGB override (downgraded to the nearest 256-color
index for a session without truecolor support) in place of the matching bare
`theme.py` constant everywhere a screen renders one, including every shared
rendering primitive (`screen_title`, `double_frame`, `empty_state`) and every
screen built on top of them. A SysOp sets or clears the three colors on one
draft editor under Settings > [C]olors, which shows every slot's real sample
text at both truecolor and 256-color depth for the values currently in the
draft and applies them together on Save (issue #282 replaced the earlier
per-slot preview-then-confirm screens and their separate preview).
This is deliberately narrow: every *semantic* color in `netbbs.rendering.
theme` (errors, warnings, success, good news, privilege badges,
operational alerts, verified-identity badges) stays fixed everywhere, never SysOp-configurable --
a caller who has used several NetBBS nodes can keep trusting that red always
means failure and green always means verified/success, regardless of any
node's own branding. Full palette theming (every color configurable) was
considered and rejected on exactly this basis, not merely deferred as too
large -- see issue #163.

A SysOp may additionally give the node name itself -- the breadcrumb segment
shown in the upper-left corner of every screen -- a per-character gradient
(issue #175), the same flair the default welcome banner's own wordmark
already gets, from Settings > Node Name > [G]radient. This is a different
kind of override from the three branding-color slots above: it recolors one
specific piece of text, not a semantic color slot standing in for a theme
constant, so it's a fixed preset list (`netbbs.rendering.gradient.
GRADIENTS`'s own keys) rather than a fourth RGB slot. It's also resolved
once at login and cached on `Session`, unlike the three RGB slots' own
per-screen `db` lookup -- it shares `node_display_name`'s existing
resolve-once lifecycle instead, since a SysOp's own change should take
effect for new connections the same way a rename does, not live-update a
session already in progress.

The welcome-banner/masthead mechanism extends to three more session-
lifecycle points (issue #177): a logoff banner, shown above the ordinary
"Signed out"/"Goodbye!" message on an intentional Log off only (never on
an idle timeout, kick, or account revocation); and a pair of new-account
banners bracketing self-service registration, one shown once before the
signup workflow begins and one once it completes successfully (covering
both an immediate login and a pending-approval outcome). Both Telnet/web
and SSH's own, separately-implemented registration paths show the
before/after banners; SSH's version reaches them through `send_auth_
banner` (before) and a kbdint challenge's own `instruction` field
(after) rather than an interactive screen, since SSH authenticates at
the protocol layer with no such screen of its own. Each of the three is
its own independent singleton, reachable from Settings > Session
banners, with no built-in default art -- disabled is a complete
non-event, unlike the welcome banner's own always-renders-something
default. Each singleton's Gallery is purpose-specific: registration
before, registration after, and logoff use distinct compositions and
copy suited to that moment rather than re-labeling the main-menu
masthead collection.

Every caller-confirmed Log off also shows a final call summary immediately
before the ordinary signed-out notice. It uses the same persisted session row
that owns disconnect cleanup to report the node-formatted connection and
sign-off times, elapsed time online to the second, and the negotiated terminal
size/color depth. The summary has no SysOp toggle. It is limited to voluntary
logoff: cancellation of the confirmation, idle timeout, kick, drain, account
revocation, and exceptional disconnects do not show it. A configured logoff
banner precedes the summary so the call statistics remain the final screen
before the connection's signed-out notice. Confirmed truecolor gets the full
gradient composition; 256-color and ASCII-style terminals retain the same
width-safe information hierarchy with compatible colors and glyphs.

The main-menu masthead (issue #161) also extends to the three top-level
index/listing screens -- board list, file areas, and the chat channel
picker (issue #176) -- since each renders once per view as a
`screen_title` + listing through the shared `netbbs.net.picker.
pick_item`, structurally identical to the main menu despite being a
recursive, categorized/Community-scoped browsing hierarchy rather than
one flat screen. Each masthead shows at *every* level that hierarchy
reaches (the unfiltered top level, a category, a Community's scope), not only the very first screen -- it marks "you're in this
section," not one specific screen state. This required `pick_item`
itself to grow a `masthead` parameter (threaded through its own internal
redraw closure so the masthead survives paging/search/sort/refresh, not
just the first paint) rather than each of the three call sites prepending
it independently, since `pick_item`'s live picker redraws itself
wholesale on every state change, unlike the main menu's own single
per-loop-iteration draw. Deliberately never the per-board/per-area
drill-down screens or the inside of a live chat channel -- see issue
#176's own scoping discussion for why those are a bigger feature and a
categorically different rendering model, respectively, not simply "one
level deeper." Each of the three is its own independent singleton,
reachable from Settings > Section mastheads, with the same no-default-
art/complete-non-event-when-disabled shape issue #177's own banners use.
The main-menu, board-list, file-area, and chat-picker Galleries are four
separate curated libraries. Their sample art must identify the surface
through silhouette and content as well as color; one generic strip
presented under four category names is not a valid sample set.

The project intentionally provides two composition paths:

- a robust simple/line-oriented editor available everywhere;
- a nano-like fullscreen prose editor as a convenience preference.

The line-oriented path is a real editor, not merely repeated irreversible
prompts: callers can revisit, insert, replace, and delete already-submitted
logical lines. Composition is separate from commitment. Mail and new posts
show a final review state from which the caller may revise address/subject/body
as applicable, commit explicitly, or cancel; leaving either editor never sends
or posts by itself. Fullscreen-editor output passes through the same review
boundary so editor preference cannot change send/commit safety.

Composing is a screen of its own (issue #813). A new message, reply, post or
edit opens under its own title ("New message", "Reply", "New post", "Edit
post") with its breadcrumb, and the To and Subject prompts are asked there,
never under the menu they were chosen from. Mail's To prompt says what to type
in plain words -- a user name, or `name@TheirBBS` for a linked BBS -- and an
empty line or Esc cancels. The fullscreen editor takes an optional header --
a title and `label: value` rows (To and Subject; Board and Subject; a file's
name) -- drawn above the text on every repaint. Its rows come out of the
text's, never the status line's, and it gives them up before the text drops
below four rows: the rule first, then the title, then fields from the last.
Review keeps its title, To and Subject on every page and pages the body with
the detail-panel machinery `show_detail` uses (`render_sections`/`paginate`),
turned with `PgUp`/`PgDn` and `[N]ext`/`[P]rev page` (`[>]`/`[<]` where the
commit key is already `P`). It stays its own loop rather than becoming a
`show_detail` caller, because it keeps its `>` cursor over To/Subject/Body and
its Ctrl-H field help. Once the body is paged, the menu is the packed action
bar, the rule §3.5 sets for a detail screen with a described menu. An outcome
carried into review, such as a refused Send, is wrapped and counted against
the page. Review's `To:` names the recipient as the node knows them: a local
account by its own spelling (`Alice`, however it was typed), a Link address
by its node's current label. Send still re-checks what was typed.

Size limits are enforced where the text is typed, in characters (issue #812).
Storage limits are UTF-8 bytes, which mean nothing to a caller -- 200 bytes is
as few as 100 accented letters -- so every refusal says how many characters to
remove, never a byte count. The Subject prompt refuses an over-long subject
when Enter is pressed and reopens on it to be shortened; an empty subject is
asked for again with Esc to cancel, except at a board's fresh prompt, which
offers Enter as its way out ("Subject (or press Enter to cancel)"). The
editors stop a body at its limit, and because the signature is appended after
them, review re-checks subject and body on arrival, says what is over, and
refuses the commit until it is fixed. The domain's byte checks remain the
backstop for every other caller.

The line editor writes paragraphs (issue #814). A blank line is a paragraph
break; a second blank line in a row, or `/done`, finishes into review, and
that closing blank is not kept. `/insert N` moves where typed lines go --
before line N -- and they keep going there, one after another, until `/end`;
the prompt numbers the line being written and `/list` marks the spot. Before
#814 the first blank line finished, so a paragraph cost an `/insert` and so
did every line of an answer written between a reply's quoted lines. Rejected:
ending only on `/done` (a blank line would never finish, against the gesture
callers already use) and a separate "answer mode" for quotes (a second
concept for what one sticky insertion point already does).

Trimming a reply's quote (issue #837) took one `/delete N` per line -- 17
commands for a 19-line quote in the first-caller field test. `/delete N-M`
removes a range, and `/unquote` removes the whole quote: every `>` line, the
attribution directly above a run of them (`netbbs.quoting.is_attribution`)
and the blank line `quote_body` leaves under it, keeping answers written
between quoted lines. That is also how a reply goes out without a quote:
[R]eply always quotes, and one command drops it, rather than a second reply
key or a question before the editor opens (§3.5). The editor says so when it
opens on a quote, and its opening lines and `/help` point to Profile's
fullscreen editor, which nothing in the line editor mentioned before; the
line editor stays the default, since it works on every terminal.

Board posts and mail distinguish discarding from saving. `/cancel` (line
editor) or discarding (fullscreen editor) always deletes any in-progress
draft; `/exit`/`/quit` (line editor) or "Keep draft & exit" (fullscreen
editor) instead keep it, and return without committing. Both editors keep a
draft as it is typed -- the fullscreen editor by its autosave, the line
editor on every change -- so a dropped connection keeps the text too.

Every draft slot belongs to one composition, and a draft is only ever
offered for the composition it belongs to (issue #814): a board's new post
(one per caller and board), a reply to one post, an edit of one post, a
caller's new letter (one per caller), a reply to one message, a forward of one
message (issue #822), a resend of one sent message (issue #825). A caller that
offers its draft itself -- a board's `[D]raft`, mail's `[D]raft`, `[C]ompose`,
`[R]eply` or `[F]orward` -- passes the draft in as the text with `offer_recovery` off, so
the editor neither asks again nor deletes the draft before something replaces
it. Before #814 mail had one body-only draft per user, which the fullscreen
editor offered in place of any later letter's text: a reply to someone else
lost its quote to it, and "Keep draft & exit" answered "Message cancelled."

A letter's To and Subject are kept beside its text in a `.fields` file (JSON:
`to`, `reply_address`, `subject`), written before the editor opens and again
before review's `[B]ody` reopens it, so the letter resumes addressed as it was
left -- a Link reply to its stored `user@<fingerprint>`. The mail screen shows a
kept new letter ("You have an unfinished letter to bob: Lunch?") and a
`[D]raft` entry (resume, delete, or leave it); `[C]ompose` while one exists,
and `[R]eply` to a message with a kept reply, offer the same choice before
asking anything, `[D]iscard` there deleting the draft and starting afresh.
A resumed letter opens on the compose screen with To and Subject shown and
the editor on its text. A body-only `mail_<id>.draft` from before #814 becomes
the caller's new letter and asks for To and Subject when resumed. A board
post's draft keeps only its text; its subject is asked again.

A board with a saved new-post draft for the caller shows a notice and a
`[D]raft` entry on its own menu (resume, discard, or leave it) instead of
interrupting entry with a question; `[P]ost` while such a draft exists goes
through the same resume/discard choice. Re-opening a specific post for edit,
or replying to the same post again, offers to resume its own saved draft
through the editor's recovery prompt. Callers that never opt into a draft
target keep the discard-only behavior -- `/exit`/`/quit` are not recognized
there at all. The fullscreen editor's Ctrl+G help says where a kept draft is
offered again.

The fullscreen editor is one preference for every long text a caller writes
-- mail, posts, bio, signature and file descriptions -- and its Profile label
says so: "Fullscreen editor (all writing)". Its keys beyond
nano's Ctrl+O/Ctrl+X/Ctrl+G (issue #815):

- **Ctrl+K** cuts the cursor's line; pressed again straight after, it adds
  the next line to what was cut. **Ctrl+Y** pastes the cut lines above the
  cursor's line (mid-line, it ends the line there), as often as wanted.
  Cutting the last line empties it. Paste is readline's Ctrl+Y rather than
  nano's Ctrl+U because Ctrl+U clears the line at every other prompt
  (issue #812), and a key that erases there must not insert here.
- **Ctrl+W** deletes the word before the cursor and the spaces between
  them; at the start of a line it joins the line to the one above.
  **Alt+Backspace** (ESC then 0x7F or 0x08, `EditorKeyKind.WORD_BACKSPACE`)
  does the same, because a browser keeps Ctrl+W for closing the tab and
  never delivers it to the web terminal. Ctrl+Backspace is not offered:
  most terminals send it as 0x08, the byte many BBS clients send for a
  plain Backspace, so it cannot be told apart.
- **Ctrl+R** rewraps the quoted paragraph under the cursor -- the run of
  lines at the same `>` depth, ended by an empty `>` line, a line at another
  depth, an unquoted line or the `[...]` of a cut quote -- to the screen's
  width, at most 72 columns, every line keeping the quote's prefix. The
  editor never hard-wraps otherwise; a quote is the exception because a
  quoted line wider than the screen shows its continuation without `>`.
  Long words such as URLs are kept whole. Off a quote, Ctrl+R rings the
  bell. nano's justify key, Ctrl+J, is the byte of Enter (LF) and cannot be
  used.
- **Ctrl+E** erases the whole text after a one-key yes (issue #837):
  clearing a bio meant holding Delete. The erased lines become the cut
  lines, so Ctrl+Y puts them back. On an empty text it rings the bell.
- The status line counts **characters used/limit**, never bytes (issue
  #812): the limit is how many characters the text holds if the rest is
  plain letters, so it drops by one for each two-byte character typed and
  reaches the count when not even a plain letter fits. It stands before the
  key hints, so a 40-column screen cuts the hints, not the count.

A cut, paste, word delete or rewrap is an ordinary edit: autosave and
"Keep draft & exit" write the resulting text to the draft, and a paste or
rewrap that would pass the limit is refused whole with the bell.

In-context help is a single shared rendering primitive
(`netbbs.net.help_overlay.show_help`) reused by two different key
conventions rather than one universal key: Ctrl+G inside the fullscreen
prose editor (nano's own Help convention, listing its keybinds and
explaining save-draft/resume), and Ctrl-H at ordinary hotkey-menu SysOp
screens built on the shared draft-based field editor
(`netbbs.net.resource_editor`), showing whichever fields on that screen
have help text authored. The two keys differ because Ctrl-H and Backspace
share one byte (0x08): safe to repurpose at a single-keystroke menu, where
Backspace already has nothing to act on, but not inside a real text editor,
where 0x08 is live backspace-editing. Authoring help text per field is
incremental, not required for every field up front.

Ctrl-C (confirmed with Thiesi, dogfood question) is an *incremental*, not a
universal, cancel key: `char_input.read_key()` returns it as a distinct
`CANCEL_KEY` sentinel, the same "return a distinguishable value, let call
sites opt in" shape `REDRAW_KEY`/`REFRESH_KEY`/`HELP_KEY` already use,
wired in screen-by-screen wherever an existing cancel affordance
(`[B]ack`, `[C]ancel`) already exists rather than swept across every
prompt at once. Deliberately does not touch `read_line()`'s editable
path in this pass -- unlike Backspace's byte, Ctrl-C during real
free-text entry has no single safe meaning across every caller (a bare
blank line already means something different per caller, e.g. a
paragraph break in the line editor, not "cancel"), so real-text-entry
cancellation is left for a later, separately-scoped increment. A
screen with no cancel affordance at all, or one that hasn't adopted
this yet, simply bells for Ctrl-C like any other unrecognized key.

Conventional yes/no prompts act on one key: `Y`/`N` immediately, or Enter for
the displayed default/current value. This uses a confirmation-specific input
primitive; generic single-key menus retain their deliberate rule that Enter is
not a menu action. Unsupported confirmation keys are rejected rather than
silently selecting a default.

Color is semantic rather than decorative state: labels, values, actions,
metadata, warnings, and success/failure states use shared theme roles across
screens. Truecolor is progressive enhancement with a deliberate 256-color
fallback, never a requirement for understanding a screen. Product polish is
bounded by named mature surfaces rather than an open-ended theming rewrite.

ANSI art editing and prose editing are separate concerns. Syntax highlighting,
spell checking, and similar enhancements remain optional modules rather than
core editor assumptions.

### 3.3 Product presentation and default visual identity

Presentation work proceeds as bounded vertical product increments rather than
waiting for every later roadmap phase. NetBBS ships one intentional default
visual identity before it grows an arbitrary theme engine. The default should
feel like a modern terminal application with BBS character: restrained color,
clear hierarchy, generous spacing, compact panels and badges, and recognizable
NetBBS branding rather than a wall of prompts or ornamental ANSI everywhere.

Ordinary screens share a small rendering vocabulary: title/breadcrumb,
sections, responsive menu grids, metadata, status messages, empty states, and
action hints. These primitives return styled terminal strings and never absorb
domain behavior. A screen should make location, content, and available actions
clear at a glance.

Layouts target 80x24 as the classic baseline, use additional width when
available, and collapse to one column at the 40x24 minimum. ASCII structure is
the universal baseline because Telnet terminal encodings vary; Unicode box
drawing must not be required for correct layout. Truecolor remains progressive
enhancement. Motion, gratuitous full-screen clearing, and decoration which
delays interaction are avoided.

The first-impression surface is one coherent product slice: generated default
banner, login, authenticated greeting, and home menu. Later increments apply
the same vocabulary to Communities/boards, mail/files/search/directory, chat,
and the SysOp operations console. Every increment covers narrow, ordinary, and
wide terminals plus empty, populated, warning, and error states.

After authentication and before the home menu, an enabled node shows a
previous-callers splash drawn from persisted session history. It lists at most
10 connections which preceded the newly opened session, preserves the existing
per-account name-visibility policy, and waits for one keystroke before
continuing. The list shortens when the negotiated terminal height cannot hold
all 10 rows together with its title, frame, and pause; an empty history skips
the splash rather than stopping the first caller at an empty screen. Confirmed
truecolor is a progressive visual enhancement with a deliberately polished
256-color fallback. The SysOp can toggle the splash node-wide from Settings;
new and upgraded nodes default to showing it. The same setting also offers a
plain style (issue #841): the node's header colour, no gradient, and the
heading "Previous callers" / "Who has called in lately" instead of the neon
"signals received" wording, for a node whose tone the neon clashes with. It
applies to the splash and the menu screen alike.

The same roll is a home-menu screen of its own, `P[r]evious callers`, rendered
by the one renderer the splash uses so the two can never disagree about who is
listed or under what name. The screen differs from the splash only where being
asked for rather than offered demands it: the node setting governs the
automatic splash alone, so the menu screen ignores it; an empty roll says so
instead of drawing nothing; and at least one row is kept on a terminal too
short for the splash's own budget. The viewer's own live session is excluded in
both places.

The home menu's `[H]istory` is the complementary per-caller screen: the
viewer's own recent calls, with connect time, how each one ended, and its
duration. It asks no name-visibility question, because the rows are the
viewer's own; that policy governs only the node-wide roll, where other callers
read it. The two screens were one listing until issue #592, which meant
`[H]istory` described itself as "your recent sessions" while showing every
caller's.

### 3.4 SysOp operations console

The SysOp entry point is an operational control center, not a flat catalogue
of administrative forms. Its landing view summarizes the running node's mode,
active sessions, Link health, moderation queues, backup and update recency,
outbound failures, and recent Link diagnostics using concise semantic status.
The standalone admin CLI renders the same view but states clearly that live
node controls are unavailable rather than pretending the process is online.
A staff member (§5.6) reaches a reduced form of this console holding only the
screens their staff permissions cover.

Navigation separates four operator intents: users, content, operations, and
settings. Operations contains observation and intervention for the live node,
Link, outbound work, diagnostics, recovery, and backups; settings contains
durable configuration such as presentation, update checks, timestamps, and
trust policy. Context-sensitive quick actions may lead directly from the
landing view to node, Link, outbox, and backup screens. Hidden capabilities are
not advertised when their runtime context is unavailable. The dashboard can be
refreshed explicitly, and action screens return to the console without losing
the operator's place.

Status context in the console lives where it is relevant. The landing
dashboard and the five top-level consoles — Users, Content, Operations,
Settings, Node — each show a full panel of what's actually relevant there: live
counts, health badges, or current configuration values. Backup state appears
on the dashboard, the Operations panel and the Backup screen; update-check
outcomes on the dashboard, the Settings overview and the Update screen. Nested
screens repeat neither. Issue #206 once put a condensed "Backup:" line on every
nested screen; the 2026-09-28 field test (issue #845) found that a first-day
SysOp read "Backup: never" under Communities, boards and banners as an error
about those screens, so it was removed.

A console screen that shows facts shows them as a *detail panel*
(`netbbs.rendering.detail`), the read-only counterpart of the draft editor's
field list: facts are grouped under uppercase section headings with a blank row
between groups; a label, its value, and a heading are three different colours;
every value on the screen starts in one column, a long one wrapping under where
it began; a state reads in the colour of what it means (a healthy one green, a
lapsed one amber, a missing file behind an enabled banner red) rather than in
the colour of the row it is on; and anything with more than one entry is a
table with a header row, its widest column wrapping rather than being cut.
Short related facts may share a row two to a line where the terminal is wide
enough. A `Label: value` sentence printed in the terminal's default colour is
not a way to show a fact.

No console screen is taller than an 80x24 terminal, the landing page included:
it draws its full health panel over a described menu where that fits, and
otherwise the same facts one group to a row over an undescribed menu, saying
that descriptions were hidden. `scripts/sysop_gallery.py` renders every console
screen and flags any that is too tall, and `tests/
test_sysop_console_presentation.py` holds the same list of screens to the
terminal's size. A panel that does not fit is
paged (`netbbs.net.detail_view.show_detail`): whole groups are kept together, a
group taller than a page repeats its heading where it continues, `PgUp`/`PgDn`
always turn the page, and `[N]ext`/`[P]rev` join the action bar — `[>]`/`[<]`
on a screen that already uses those letters. A list that grows without bound
(an account's admin actions, trust configuration history) is a screen of its
own rather than the tail of another. A detail screen that keeps its own
described menu gives the menu only the rows the panel leaves, and falls back to
the packed action bar before it lets the panel's top row scroll away. The
banner and masthead screens do the same (issue #662): each fits at 40, 64 and
71 columns, and the board-list, file-area and chat-channel mastheads, which ran
three rows past 80x24 before, fit there too.

**Known limit below 72 columns (issue #662, decided).** Below 72 columns
`menu_grid` gives each entry two rows and paired fields go one to a row, and
the packed-bar fallback is not paging. Five screens are still taller than 24
rows there, measured with the gallery: the landing dashboard (34 rows at 71
columns, 35 at 64, 40 at 40), a board's detail (28, 28, 34), and a user's, a
file area's and a channel's detail (25 at 64-71, 27-28 at 40). With redraw in
place on, their tops scroll away. This is accepted: a SysOp running the
console below 72 columns is rare, and fitting these screens would mean paging
the detail screens (losing their described menus and moving the user detail's
field cursor across pages) and a third, narrower dashboard layout. Paged
screens are not affected. Revisit if a SysOp actually works that narrow.

A menu too short for a description under each entry puts each one on its
entry's own line, cut to fit, before it hides them (issue #840): at 80x24 the
first field test's SysOp read "Descriptions hidden" on the console landing,
exactly where one-word entries such as Content and Operations needed them.
Only when even one line per entry does not fit are they hidden, with the note.

Every new account starts with redraw-in-place on, however it was made:
signed up, created in the console, or the first SysOp created at install
(issue #840). The first SysOp was the one left out, so a node's own SysOp saw
screens scroll that every one of her callers saw redrawn.

The outcome of an action is carried into the next redraw; it is never written
somewhere that redraw erases, and the console never asks for a keypress just to
keep a result on screen. With redraw-in-place on, a line printed just before
returning to a menu is wiped by that menu's clear before it can be read. So a
console action *announces* its outcome -- `'General' deleted.`, a rejected
field value, "Cancelled.", an empty list's "No message boards yet." -- and
whichever console screen is drawn next shows it directly above its prompt: a
menu, a detail panel, a draft editor, or a picker. A picker used to draw it
with its masthead, above the title, which started the whole screen a row down
until the next redraw (issue #964); it is drawn under the list's keys like
everywhere else. It is shown once, to the
session whose action produced it, and is gone at the next redraw. What is more
than a line is a screen of its own instead: a maintenance action (prune drafts,
GC storage, repair carried posts), an empty log, and one entry's detail are
each titled and held until `[B]ack`. The pauses that remain are for looking at
something -- a banner preview, a door's service log -- not for reading a
result. A flow shared with first-run onboarding (managed-DNS register, release,
rename, cancel) writes its own outcome; the console runs it behind a stand-in
session that holds what it wrote after its last question and announces that.

### 3.5 Interaction model for screens (issue #282)

Every screen reached by a hotkey shows its content first and can be left with
`[B]ack` (or a pause, `[Enter] Continue`) without answering a question or
changing any stored value. Actions are hotkeys on an action bar, a field on a
draft editor, or a picker entry; a yes/no prompt is only ever the last
keystroke immediately before an irreversible, destructive, or network-touching
action, and it sits behind a hotkey the caller chose rather than on the entry
or exit path. A toggle is a hotkey that toggles (or a live editor field), not
a "Turn X on?" question that doubles as the exit. A "blank keeps the current
value" prompt writes nothing else, including a sibling visibility flag.
A text field opens on its current value instead (issue #529): Enter saves what
is shown, an emptied line clears it, and Esc leaves it unchanged -- "keep" is a
key rather than an overload of the empty string. Ctrl-U empties the line at
every single-line prompt on every transport, masked ones included (issue #812),
so clearing a long value is one key rather than one Backspace per character. Width is no longer a reason to fall back (issue #546): the line editor
keeps a one-row window over the buffer and scrolls it to follow the cursor, so a
value wider than the terminal is edited like any other. A value longer than the
editor's own buffer cap still falls back to the older prompt, blank-keeps-it and
all, and says so.
Every single-line prompt scrolls this way, not only those that ask for it (issue
#964): an answer that outgrew the row after a long prompt used to wrap onto a
second row, where Backspace could not reach the text before the wrap. A prompt
that passes no window gets one sized to the columns its prompt left, read from
the session's screen copy. While the answer fits it echoes character by
character as before; the keystroke that would reach the edge hands over to the
window. A masked answer stops showing `*` at the edge instead, so it never
wraps either.
A key a caller can press is highlighted wherever it is offered, not only on
menus (issue #974): a prompt that lists its choices in running text
("Unsaved changes. [S]ave, [D]iscard, or [C]ancel?") and a detail-panel label
that names a key ("[R]otate") colour the bracketed key as a menu entry does
(`highlight_hotkeys`). Issue #1083 extended this to outcomes and reports: a
notice's "Use [P]review to verify it looks right." highlights its key too, in
the notice helpers themselves (`notices.announce`, the console's
`_announce_line`), and a report line (a `[C]heck` verdict, an art's size) puts
its numbers in the emphasis colour (`highlight_report`). A saved file's path
reads in the value colour, and a SAUCE credit tells title, author and group
apart. Only help pages still write keys plain. A test keeps prompts and menu
labels from writing a bare `[X]word` again.
With redraw-in-place enabled, the selected text, optional-integer, age,
integer, float or optional-text field is edited at its displayed value row.
The existing Choice row carries the editing key hint; no typing prompt opens
below the action bar. Wrapped continuation rows are cleared while the full
buffer is edited through a horizontal viewport, then restored on redraw.
With redraw-in-place disabled, the editable prefilled prompt remains below
the form. A field hotkey on another section displays that section before
editing. If the form is taller than the terminal, editing temporarily shows
the portion containing the selected field. A terminal resize cancels the
unsubmitted edit, preserves the draft and displays an explanation after
laying out the new dimensions.
Anything gathering more than two values goes through the draft field editor or
a picker and persists nothing before `[S]ave`. The deliberate exceptions are
once-only first-run decisions (Link participation, node name, managed DNS,
the character-set question after login, issue #929), type-the-name confirmations before deletes, and
masked credential entry (issue #611): a password is typed twice because the
caller cannot see it, preceded by the current one where the account acts on
itself, and a draft editor would have to hold the plaintext across redraws to
offer anything more. Each prompt cancels on a blank line and nothing is
written before the last one.

**A console resource opens on its own fields** (issue #1081). The six resource
screens of the SysOp console -- a Community, a category, a message board, a
file area, a chat channel and a door -- are one screen each, not an overview
with an `[E]dit` key in front of the draft editor. The screen shows what can't
be edited first, in a compact header of one or two rows (counts, place in the
callers' list, Link status), then the editable fields with the cursor already
on the first one, then the actions. Long Link details (origin, closure, pending
transfers, peer reach) form a "NetBBS Link" section after the fields. A field
is chosen by the cursor only: ↑↓ to choose, Enter or Space to change it, ←→ to
step a value. Fields have no letters on these screens, so the action keys
(`[U]p`, `[R]emove`, `[P]ending posts`, `[L]ink`, `[S]tart service` and the
rest) keep theirs. Changes go into a draft as on every other editor; nothing is
stored before `[S]ave`. Once a field differs from what is stored, the action
bar shows only `[S]ave` and `[B]ack`, so no action runs against values the
screen no longer shows. `[B]ack` leaves at once while nothing has changed. With
changes it discards typed work, which can't be undone, so it is the
hotkey-chosen, destructive action the yes/no rule above allows a question for,
the same "Discard unsaved changes?" every draft editor asks. On a Linked
resource this node is not the origin of, a field the origin controls is shown
in place, read-only, labelled "set by origin". Creating a resource uses the
same screen with an empty draft and no actions. Account screens, settings
screens and caller-side editing (a post, a file description, the Profile) are
unchanged: an account's keys are separate, individually confirmed operations
rather than fields of one form, and the settings screens already open straight
into their editors.

A pause that waits for a key before going on reads `[Enter] Continue` (issue
#1083), or `[Enter] Back`, `[Enter] Stop` where that says more. Written
text gets it from `netbbs.rendering.continue_prompt`; the live screens that
paint cell by cell (the Monitor, break-in) from `live_screen.paint_keyed_text`,
which colours the key the same way. Any key still goes on. The
bracketed `[Enter]` is what a click in the browser terminal sends, so a
caller using only a mouse is never stuck behind a pause; the old wording,
"Press any key to continue", had nothing to click.

**An action's outcome is shown on the screen the caller lands on** (issue
#680). With redraw-in-place on, a line written just before a screen redraws
is erased by that redraw's clear. So an action does not write its outcome
("Posted.", "Could not send: ...", "Sent 'game.zip'.", a one-time transfer
link); it announces it through `netbbs.net.notices`. Whichever screen is drawn
next shows it directly above its prompt, and a picker shows it above its
list. No keypress is asked for. This started in the SysOp console and now
applies to every screen: boards, file areas, the composition review screen
shared by posts and mail, every picker, the main menu and the mailbox that
flows unwind back to.

A screen with a nothing-to-do state still draws a `[B]ack` bar and waits,
rather than returning straight into its parent's redraw, where it would flash
and vanish. The one exception is a picker with nothing to pick: it announces
its empty message and returns, so the screen it returns to says it.

A picker row carries one number: the one that selects it on this page
(issue #838). Rows used to show a second, permanent `(#N)` reference -- the
item's database id -- for a `[G]oto #` command, so "02. (#1) Fountain Pens"
asked a first-time caller to tell two numbers apart before choosing, and the
field test found it confused more than it helped. Both are gone, on caller and
SysOp screens alike. A number that keeps meaning the same item is the list's
own order holding still (#839), not a second number beside it. The picker still
identifies each row by a stable id internally, to reopen a list on the row just
left. A caller key that acts on a row (New scan's `[M]ark read`) takes the
highlighted row, or asks for its number on the page.

A row number is two digits, or one digit and Enter (issue #840): the first
field test's newcomer typed "3" and Enter where "03" was wanted, and nothing
happened. A whole word typed at a one-key prompt ("Communities", "no") acts on
its first letter only: after a main-menu key or a yes/no answer, letters that
follow within 0.6 seconds of each other, and the Enter that ends them, are
dropped rather than read by the next screen as keys (`char_input.
arm_word_guard`). Any other key, or a pause, ends that at once. In the browser
a click on a menu entry sends its bracketed key and a click on a numbered row
its number; a click on anything else says once that the terminal is driven by
the keyboard. The browser is never asked the plain-ASCII question, since it
always draws Unicode. `[?] Help` on the main menu (and Ctrl-H there) sums up
the keys, Back, New scan and who runs the node, with the User Handbook's
address, and E-mail to `sysop` reaches the node's first usable SysOp account
unless an account has that name.

No *menu* has a typed command language. A caller's options are the keys the
action bar shows, and a prompt reading `Choice: ` accepts exactly those. The
file-area listing was the last menu that read whole *lines* instead: it
predated its own keystroke support and carried `/download <name|#>`,
`/upload`, `/describe <file>`, `/weblink` and `/remote` alongside the keys,
a dialect nothing else in NetBBS spoke and nothing on screen taught. Those
are removed. `[D]ownload` and `[E]dit description` act on the file under the
cursor, on the only file on the page, or on whichever one a picker returns —
one shared resolution, so two hotkeys on the same screen cannot disagree
about what "this file" means.

A free-text field is not a menu and is the one place a `/`-command belongs:
its ordinary content is prose, so a command needs a sigil to be
distinguishable at all. Chat's `/msg`, `/private` and friends stay, as do
the editors' `/done`, `/exit` and `/help`.

What a typed filename could reach and a keystroke cannot, recorded because
it was a deliberate trade and not an oversight:

- **A file on another page.** `[/] Find` covers it: searching enters the area
  with that file at the top of its page, where its number or `[D]` takes it.
  (It is the first row, not a preselected cursor — `_show_area` starts with
  no highlight.)
- **A file awaiting approval.** The listing carries approved rows only, so a
  pending upload was reachable only by name. Two keys replace that: `[E]` on
  the listing offers the caller's own waiting uploads for description, and
  the SysOp's pending-file review screen gained `[D]ownload`, which is where
  inspecting an upload before approving it belongs anyway — a moderator
  reading its `FILE_ID.DIZ` and then approving the bytes unseen was the
  weaker half of that screen all along. It is offered only when the
  transport can carry a Zmodem send or the node can mint a browser link,
  the same rule the caller-facing screens follow (issue #475).
- **An expired file.** Nothing, and that is now the decided answer rather
  than a loss: expiry ends a file's reach to callers entirely (§5.3, issue
  #639). The listing and `[/] Find` are approved-and-current only, and a caller
  who knows a name has no way to spend it. A SysOp reaches an expired file
  while the grace period lasts through `E[x]pired files` on the file area's
  admin detail screen, which carries the same `[D]ownload` (§5.3).
  Only areas that set a maximum file age have expired files at all.

### 3.6 Resource lists (issue #528)

Every list searches with `[/] Find`, the main menu's key and word (issue
#1083): one key for searching wherever a caller is. A list's Find narrows it to
the names containing the text typed, and a blank answer shows the whole list
again. It replaced `[S]earch` outright, with no hidden `S` alias: a silent
second key would keep `S` taken on every list for nothing a caller can see.

A list row's secondary text is either prose or a record, and the two render
differently.

Prose stays prose: a moderation reason, a log message, a peer fingerprint, an
author attribution. These render as `pick_item`'s single trailing description
string, muted, exactly as they always have.

A *record* — a row whose secondary text is several independent fields — is a
table. `pick_item` takes `columns` and `column_values_of`, and renders a
`LABEL_COLOR` heading row above fixed columns, each column separately
colored. This covers the SysOp's board, file-area, chat-channel and
Community listers. The distinction is whether the fields can be compared down
a page: levels, status and gates can, and a sentence stapling them together
prevents it.

Two rules follow from that, and are normative for any future list:

- **A resource's access gates appear wherever a SysOp lists that resource.**
  A minimum age or a name requirement changes who may enter, and a list that
  omits them shows a gated resource as identical to an open one. A gate is
  colored (`GATE_COLOR`) only when present, so an ungated row stays quiet
  and a gated one is visible while scanning. This holds in the narrow
  fallback too: a long name is bounded there rather than allowed to push the
  gates off the end of the row.

  Caller-facing pickers carry gate metadata too, since issue #541 closed
  the gap this paragraph used to describe -- but only the part of it a
  caller can act on. A channel whose name requirement they do not meet is
  still listed, and says so ("needs verification"), in both the chat
  channel picker and `[N]ew scan`. It is not hidden: a name requirement is
  a *participation* gate rather than a content restriction, unlike an age
  gate, which does hide the resource. The note is placed ahead of any
  free-form description, because the row is clipped to the terminal width
  and whatever sits at the end is what a narrow terminal loses. The same
  note marks a resource whose age gate wants a verified age from a caller
  who is old enough only by the birthdate they entered (issue #1082): that
  is the one age-gated resource a caller is shown before being refused,
  because getting verified is something they can do.

  The rest of the gate set stays on the SysOp side for now. Level and age
  already decide visibility rather than needing to be displayed, so the
  open question is only whether a caller should be told *why* something is
  absent -- a different feature from telling them why something present
  will refuse them.
- **A table that does not fit becomes prose again.** Below the width at which
  the name column stays readable, the row falls back to the flat description
  form. The decision is made per render against the live terminal width, not
  once on entry. A truncated table is worse than the sentence it replaced.

A row shows what **applies** to a caller, resolved through the Community
cascade (`get_effective_min_age` and friends), never the resource's own raw
unset value. A board that sets no age gate but sits in a Community that does is
gated, and enforcement says so; a list that printed the resource's own `None`
would report it as open. The resource's own screen in the console (§3.5) is
where a SysOp sees which values the resource itself sets. An explicit `0`
minimum age is not a gate -- `meets_age` admits everyone -- and is not tagged
as one.

In the prose fallback the gates lead the string, because a narrow terminal is
precisely where that string gets truncated: who may enter is the least
guessable fact in the row, so the levels take the truncation instead.

Cells are measured and padded in display columns, never character counts, so
a CJK name does not shift the columns after it, and whitespace is normalized
before measuring since a preserved tab measures zero but renders as a space.

Draft editors share one label-column width across the entire field list,
including sections on other pages. Wrapped values retain that same hanging
indent. When the widest label would leave fewer than twelve value columns,
all values move to a separate row with a shared two-column indent; labels and
values wrap without truncation. This layout also supplies the physical row
and column used for in-place editing.

---

## 4. Accounts, authentication, identity, and addressing

### 4.1 Account authentication

Local users may authenticate with:

- username and password, the default path;
- an optional personal Ed25519 keypair for passwordless challenge-response
  login.

The server never needs a personal user private key. Password-only users are
expected to remain the majority and must not be treated as second-class users.

A password has a lifecycle after creation (issue #611). An account changes
its own password from the Profile screen after proving the current one; an
account with no password (key-only) sets its first one without that proof,
on the strength of the login that reached the screen. A SysOp sets a new
password on any account from that account's detail screen, or from
`python -m netbbs.admin reset-password USERNAME` when locked out of the
console; neither route asks for or reveals the old password. A password may
be cleared only while the account keeps at least one public key, the same
"never leave an account with no way in" rule key removal already applies.
Every change is audit-logged with the actor and carries no other detail. A
guest session (§4.6) cannot change the guest account's password, because it
proved no credential. The current-password proof inside a session charges the
same login throttle as the login prompt, so an unattended session is not an
unthrottled place to guess.

### 4.2 Registration modes

A node has one registration mode:

- `open`: self-registration creates an immediately usable account;
- `approval_required`: self-registration creates a pending account which
  cannot authenticate until approved;
- `closed`: the public registration option is absent and accounts are
  SysOp-created.

A pending account that presents the right password is told it is waiting
for approval and the connection ends (issue #835). Any other failure stays the
generic "Login failed": the distinction is made only after the credential
has matched, so it tells no one anything they could not learn by logging in.
SSH shows the same notice as an authentication banner on a password login.
It stays generic for a public-key lookup, because SSH asks that before the
client has signed anything and a public key is public. A signup that created a
pending account is not charged as a failed login attempt on its connection.

Self-registration checks the desired username as soon as it is typed, before
the password prompts, and spends a login-throttle token doing so: whether a
name is taken is the same existence answer account creation used to give,
only earlier. Beyond the grammar every account shares, a caller may not
register:

- a reserved name: `sysop`, `cosysop`, `admin`, `administrator`, `root`,
  `moderator`, `mod`, `staff`, `support`, `system`, `operator`, `postmaster`,
  `guest`, `netbbs`;
- a name containing `sysop`;
- a look-alike of a level-255 account's name.

These names are compared by a skeleton: case folded, `_ - .` dropped, and
`0/o`, `1/l/i`, `3/e`, `4/a`, `5/s`, `7/t`, `8/b`, `9/g`, `2/z`, `rn/m` and
`vv/w` folded together. The rules apply to self-registration only. A SysOp
creating an account by hand may use any name the grammar allows. Only SysOp
names are protected, not every account's, because impersonating the operator
is the harm the persona test found. Blocking look-alikes of every caller would
refuse ordinary names for no gain. A signup is turned down with Decline on the
pending account, which deletes it after a yes/no. Deletion's typed-name ritual
guards content and Link history that a never-approved account cannot have.

On an approval-required node the SysOp may set one signup question, up to 200
characters. Self-registration asks it after the password. The answer is
optional, cut to 300 characters, and stored with the question as asked. It is
shown on the pending account's detail screen and deleted when the account is
approved. It was given for that one decision, and keeping it would build a
profile nobody agreed to. Declining removes it with the account. An open node
never asks it, because nobody reads the answer before the account is usable.

Registration determines whether an account may exist and log in. Link
probation and reputation determine what an active identity may do; these are
separate axes.

### 4.3 Account levels and the usable-SysOp invariant

One integer level drives ordinary level gating. `SYSOP_LEVEL = 255` is the
reserved top level; SysOp is not a parallel role flag. Levels below 255 grant
no authority by themselves. Authority short of SysOp comes from staff
permissions (§5.6) and moderator grants (§5.2), never from a level band.

Promote, demote, disable, enable, approve, and hard-delete operations must never
leave the node with zero **usable SysOps**. A usable SysOp:

- has level at least `SYSOP_LEVEL`;
- is not disabled;
- is not pending approval.

The invariant is enforced transactionally against fresh database state, not
against a stale object supplied by a caller.

A change to an account's level, its verify-identity permission or its staff
permissions (§5.6) applies to that account's live sessions without a
re-login (issue #659), whichever process made it. Each session's account watcher re-reads the account every
few seconds, and an in-node change wakes it at once. A gain is picked up the
next time the main menu is drawn, straight away if the caller is sitting on
the menu, and nothing is interrupted. A loss interrupts the caller's current
screen and returns them to the main menu, redrawn for the new level with a
line saying what changed. Every screen was entered under the old access, the
SysOp console above all, and interrupting is the only way to reach a screen
that is waiting for a key. The interruption ends whatever the caller was
doing, a running door included. An editor keeps its text as a recoverable
draft. The SysOp console also re-checks its operator at its own menu, which
is what stops a demoted operator in the standalone CLI. Moderator grants
(§5.2) need no watcher: they are read from the database at each check, so a
grant or a revocation governs the holder's next action in every session.

**Automatic promotion** (issue #992). A SysOp can set promotion rules
(`Users ▸ Promotion rules`). Each rule raises an account from one level to a
higher one once the account is old enough, has logged in often enough and,
optionally, has posted enough.

- **When.** Rules are checked at login, after the login is counted and before
  the session takes its level. The session starts at the new level and the
  caller is told once, on the first main menu. An account that qualifies
  while away is promoted at its next login, the first time the level
  matters. Nothing runs on a timer.
- **Shape.** One rule per starting level, so which rule applies is never a
  guess. A rule never demotes and never reaches 255. One rule applies per
  login, so an account climbs a ladder of rules one step per call.
- **Who is left alone.** The guest account (whether guest login is on or
  not), pending and disabled accounts, staff and SysOps, and every account
  whose level a person has set. A level set by hand takes the account out of
  the rules, so a demotion is not undone at the next login; a starting level
  other than 0 counts as set by hand. The account screen shows whether the
  rules apply and turns them back on.
- **Counting.** Logins are counted on the account itself, since session
  history keeps only a few rows per account. Posts are the account's
  approved posts on this node, each counted once however often edited.
- **Audit.** An automatic promotion is recorded in the moderation log with
  no acting account, shown as "(system)", and names the rule.

Rules are node configuration and travel in backups.

Hard deletion preserves content provenance through denormalized display labels
or nullable author/uploader references. Personal access rows and private state
which cannot meaningfully outlive the account are deleted according to explicit
foreign-key policy.

On a node that has ever run NetBBS Link, hard deletion also retires the
username (issue #594). The opaque local user identifier of §4.5 is the
username, so on the Link an account is its name: mail is addressed to it, a
carried post's author label is built from it, the trust subject is derived
from it, and a remote attestation names it. A freed name would hand all of
that to the next registrant. The name is recorded in the deleting transaction
and refused at registration, case-insensitively, whatever the Link setting
does afterwards; "ever" is a sticky marker set when the node first starts with
Link effectively on, and seeded at upgrade, on a node that ran Link before the
marker existed, from any artifact Link leaves behind. A node that has never run Link records nothing
and its names stay reusable. Nor is the name retired of a registration that
was still awaiting approval and was never sent Link mail: such an account has
provably never had a session, so there is no Link identity to inherit. A SysOp can release a retired name, as a
confirmed and audited action. Self-service registration refuses a retired
name in the words it uses for a taken one; a SysOp surface says why and where
to release it.

### 4.4 Human-facing Link addresses

The normal human-facing cross-node address is:

`user@friendly-name` or, where a friendly name is ambiguous,
`user@canonical-dns-name`

Link endpoint descriptors carry both claims inside the node-signed hello
bundle. User-facing screens show the friendly name, qualified by the canonical
DNS name where useful, and do not expose fingerprints by default. DNS names are
unique routing names, but neither a friendly name nor DNS is cryptographic
identity authority. Transport endpoints in the descriptor's `addresses` list
are connectivity data, not presentation claims; an older peer which omits
`canonical_dns_name` does not implicitly claim its endpoint host as a DNS name.

The underlying address and every protocol/persistence relationship remain
`user@node-fingerprint`. Durable linked content -- channel scrollback, mail,
carried board posts, fetched Link files -- therefore persists the fingerprint
and resolves the home node's *current* friendly identity when rendered, so a
benign rename is followed and nothing stored has to be rewritten. When that
technical author identity has an undismissed cryptographic-identity warning,
durable board and file attribution shows the warning and full fingerprint
alongside the friendly label; browsing remains available. A node with
no authenticated profile (one that has never been admitted, or is known only by
an administratively configured fingerprint) is shown by that fingerprint rather
than a shared placeholder. A full fingerprint is available as **Technical
identity** in the relevant SysOp detail view and remains accepted as an
advanced/backward-compatible input. Friendly-name resolution must be unique;
an ambiguous presentation name is refused, and the refusal spells out the
address to type for each candidate -- `user@<technical identity>`, with the
name that node goes by -- rather than repeating the already-ambiguous claim.
DNS and friendly claims share that one
namespace, so a reference matching one node's DNS claim and another node's
friendly claim is ambiguous rather than silently preferring either. Presentation
claims and abbreviated fingerprint input likewise resolve as one candidate set:
a name which equals another node's fingerprint prefix is ambiguous rather than
silently shadowing that technical address. Abbreviated fingerprint input counts
only from six characters, the length the node map shows (issue #807): below it
a one- or two-letter friendly name collided with every peer whose fingerprint
started with the same letters -- one peer in 32 for a single letter -- and
could never be used, while a deliberate six-character imitation of a prefix is
still ambiguous. Exact full fingerprints retain precedence. Fingerprints remain
hidden in the ordinary unique-name path.

What a caller reads after the `@` is what they can type back (issue #807). A
reference also matches a node's full display label (`Name · dns.example`; the
`·` is reserved, so a label never equals another node's name), and a node name
that contains `@` is shown in double quotes -- `bob@"Cats @ Night"` -- so a
reader can tell where the user name ends; one pair of enclosing quotes is
dropped from a typed reference, which is unambiguous because no friendly name,
DNS name or fingerprint may contain a double quote. A typed address splits at
its first `@`, since a user name cannot contain one and a node name can.

A chat line, and each line of a Link private conversation, names a linked
speaker's node by its friendly name alone (issue #899): the DNS name repeated
on every line cost more width than it told anyone. Where another node this BBS
knows of claims the same name -- as its friendly name or its DNS name, the one
namespace the identity warning uses -- or this BBS claims it now or did
recently, the name is qualified -- `Name · dns.example`, or `Name · abc123`, the first six characters
of the technical identity, when the node has no DNS name -- and the node map
disambiguates a shared name the same way. Both qualified forms resolve when
typed back. The speaker is styled in parts: the brackets and the `@` muted,
the user in the speaker color, the node in its own color (`NODE_COLOR`).

The user half of an address follows the local username grammar (ASCII letters,
digits, `_`, `-`, `.`, at most 32 characters), capitals included: a name is
addressed exactly as it is displayed, and the recipient node looks it up
case-insensitively, so `OldNib@Q` and `oldnib@Q` reach the same account. A
sender's name goes out as it is spelled; an account older than the username
rules whose name falls outside that grammar cannot send Link mail, since no
reply could reach it, and is told to ask for a rename. Friendly names are compared in one Unicode
normalization form (NFC), so canonically
equivalent spellings are one name, never two claims. UI delimiters, invisible
control/format characters, and the `Unnamed linked node` and `Unknown linked
node` fallback labels are reserved and cannot be claimed as friendly names. A
complete 32-character
base32 node fingerprint is reserved too: exact fingerprint lookup has precedence,
so allowing the same shape as a friendly name would make that claim unreachable
or misleading.

Peers retain authenticated observations of all three values. A friendly-name
change under the same fingerprint is an informational continuity notice. A DNS
change under the same fingerprint is a more prominent routing notice. Reuse of
a familiar friendly or DNS name by a different fingerprint is a strong
cryptographic-identity warning. A friendly name counts as familiar when it reads
as one -- compared by §4.2's skeleton as §6.3 extends it for aliases, the key they are
checked with, so "0utBound", "Out Bound" and "OutBоund" with a Cyrillic о are
all "OutBound" (issue #900) -- while a DNS name must match exactly: it is unique
by registration, and folding would equate different real hosts. The UI explains that recovery/replacement may
be legitimate but impersonation is possible, and it does not prevent the user
from continuing. Presentation names never transfer trust or reputation between
fingerprints.
An undismissed cryptographic-identity warning continues to be shown at
interaction boundaries even if a newer benign profile change is observed;
only SysOp acknowledgement dismisses it. Bounded observation pruning therefore
retains undismissed security warnings ahead of newer benign observations. The
file catalogue re-reads the selected origin's warning after the picker returns,
before asking for fetch consent. Trust-subject details show the same warning and
full technical identity; applying an override to a warned subject requires a
fresh, default-no confirmation of that fingerprint immediately before mutation.
Neither warning prevents continued interaction. The
local node likewise retains a bounded history of its previous friendly and DNS
claims so another fingerprint cannot adopt a just-renamed local identity without
raising the same warning. Reusing a retired claim moves it to the recent end of
that bounded history, so retention follows the latest use rather than the claim's
first retirement. The last friendly/DNS pair actually advertised in an
outbound hello remains part of that collision namespace until it has been moved
into history; an inbound hello racing the local rename therefore cannot claim
the just-replaced name in the gap before the next outbound descriptor is built.
Committing a changed local friendly or canonical-DNS claim also re-evaluates
every peer descriptor already on disk against the new claim, because a peer
which claimed that name first sends no further hello merely because this node
renamed; an identical already-recorded collision is not duplicated.
Startup primes the current local friendly/DNS pair before opening the Link
listener. Thereafter the shared own-hello cache is refreshed through the
background database lane before an inbound peer is persisted and once per
outbound sync pass. Each outbound hello repeats that refresh after its network
wait and immediately before persisting the authenticated peer; candidate
fallback uses the same path. A verified peer-list refresh likewise repeats it
after its network wait and immediately before persisting any updated known peer.
Every endpoint which accepts a signed hello, including
relay-mailbox pickup, performs that refresh before peer persistence; building
the signed response itself performs no synchronous
database I/O on the event loop. Authenticated live scrollback snapshots retain
their authors' home-node fingerprints in memory as well as their friendly
labels, so the same non-blocking identity warnings remain available even before
the corresponding durable event has arrived.

High-impact consent screens retain the same non-blocking model but disclose
security context before confirmation. Offering or accepting a board-origin
transfer involving a node with an undismissed cryptographic-identity warning
shows that node's full technical identity before the default-no prompt. If an
incoming offer's authenticated origin profile is unavailable, the confirmation
falls back to the signed origin fingerprint rather than an anonymous placeholder.
Likewise, a remote-file catalogue marks warned origins with their full technical
identity and repeats that caution before the fetch confirmation, before any bytes
are transferred.

### 4.5 Identity tiers

NetBBS has three author/identity tiers.

#### Password-only user

A password-only user has no personal cryptographic identity. Link events use a
`node_vouched_user` author reference containing:

- the home-node fingerprint;
- an opaque local user identifier.

The home node signs on the user’s behalf. Key rotation and recovery are entirely
the node operator’s responsibility.

#### Personal-key user

An opt-in user may register one or more keypairs (issue #222 — multiple
simultaneous personal keys/devices, shipped) for passwordless login, each
independently valid; none is a root/operational hierarchy the way node
identity has. Author-identity purposes elsewhere in this design (Link
event authorship, display) that need exactly one fingerprint per account
use the account's *primary* key — the first one registered, or another
automatically promoted if the primary is later removed while other keys
remain.

**Promotion is a mechanical fallback, not an identity claim (code review
follow-up, PR #225).** There is no signed key-transition chain linking a
newly-promoted key to the one it replaced — unlike node identity, whose
root key signs each operational-key transition specifically so remote
peers can verify continuity across a rotation. A promoted personal key
is, from every remote Link peer's point of view, simply a different,
unrelated fingerprint: content authored after a promotion is *not*
cryptographically provable as continuing the same reputation history as
content authored before it, even though the local account is unchanged.
This is the same limitation the single-key model already accepted below
("losing the key loses that key-based identity and reputation
continuity"), now reachable as a side effect of removing one key among
several rather than only by losing your one and only key — a real,
disclosed gap, not a hidden one, and one this feature does not attempt
to close (a signed personal-key transition chain, mirroring node
identity's, is a real future option if reputation continuity across a
personal key change ever becomes a stated goal — not built now, not
implied by anything here).

There is no bespoke recovery mechanism for any individual key. Losing
every registered key loses key-based identity and reputation
continuity; the local account may still use ordinary account recovery
policy.

#### Node identity

Every node has:

- one long-lived root key whose fingerprint is the stable node identity;
- one operational signing key for events and content;
- one operational transport key reserved for Noise-based real-time transport.

The root authorizes and revokes operational keys through signed transition
records. Historical signatures remain verifiable by walking the transition
chain back to the root.

Routine operational-key rotation and compromise response do not change the
node address. Root-key loss or compromise has no cryptographic recovery in the
current design. Social/M-of-N recovery remains a possible future extension, not
an assumed capability.

Replacing the root identity necessarily changes the fingerprint and is treated
as a new cryptographic identity. Keeping the same friendly or DNS name makes
that replacement recognizable to humans but does not establish continuity;
peers raise the strong warning above and continue to permit interaction.

Root and operational keys are generated at initial bootstrap. Rotation is a
guided SysOp action (issue #624): **Link status → Keys** on the running node,
or `python -m netbbs.admin rotate-key` on a stopped one. Either operational
key rotates on its own, and a rotation is one of two kinds, which the root
states in the revoke it signs:

- **Routine.** The old key is *retired*. What it signed while current stays
  valid, so the node's boards, posts, files and mail remain usable by peers
  that have not received them yet, including through carriers.
- **Compromise response.** The revoke carries `"compromised": true`. Peers stop
  believing anything the old key signed, and the node signs its own stored
  objects again under the new key. A key retired routinely can be declared
  compromised later by a second revoke that says so. A node that knows the
  rotating node only by introduction learns the revoke from whoever carries
  that node's content, beside the content (§8.11, issue #914). A copy a node
  already held when it learned the revoke, signed only by the compromised key,
  is held as stale from then on: not declared, not served, and replaced in
  place by the re-signed copy when a peer offers it (§8.11, issue #672).

What counts as signed "while current" is decided per object family. A
long-lived event is checked against the current key and then every key retired
without being called compromised. Anything signed fresh for one exchange (a
hello, a request, a withdrawal) is checked against the current key only. So is
anything the issuer re-issues on rotation (trust objects and attestations,
§12). §16's issue #624 entry has the rationale. Root-key custody is part of
ordinary node backup and restore rather than requiring an HSM or offline
ceremony.
### 4.6 Guest login (issue #531)

A node may designate one **existing account** as its guest identity. Typing
that account's name at the login prompt starts a session as that user without
a password prompt.

That is the whole feature, and the boundary is deliberate: guest login is an
*authentication* shortcut and never an authorization model. The guest is an
ordinary account, so levels, per-object permissions, age and name gates,
moderation, auditing and Link trust apply to it exactly as to any other
caller, and **no code branches on whether a caller is a guest** -- with the
exceptions below: mail, what a session that signed in without a credential
may change about the account it shares or keep after the call, and the
privileges the guest account may hold. A SysOp says what a guest may do the same way they say
it for anybody else: by setting the guest account's level, and by granting or
withholding per-object access.

Three consequences follow, and are intended rather than gaps:

- Writing is not blocked structurally. A guest meeting a board's write level
  may post; the level is the mechanism.
- The account keeps its password. The designation does not touch the
  credential, and turning guest access off -- one configuration change, leaving
  the account untouched -- restores ordinary sign-in for it.

  While guest access is *on*, though, typing that name on Telnet or web signs
  in as the guest: that is the feature, and there is deliberately no second
  path that offers the password prompt for it instead. The designated account
  is a node identity rather than a person's, so a SysOp who needs to act on it
  either turns guest access off for the moment or works on it from the SysOp
  console, which reaches everything about it including its keys. SSH is
  unaffected -- a key has already proven identity before the login flow runs.
- Everything after authentication still runs, and is re-checked at the moment
  of use rather than when the designation was saved. A blocked guest is
  refused. A disabled or not-yet-approved account is refused. An account
  promoted to SysOp after being designated is refused -- otherwise designating
  an ordinary account and then promoting it would be a passwordless route to
  SysOp. And a deleted guest stops being special, falling through to the
  ordinary password prompt.
- The designation records the account's **id and creation timestamp**, and both
  must match. Neither a name nor an id alone is an identity: a name resolves to
  whatever row holds it now, and `users.id` is `INTEGER PRIMARY KEY` without
  `AUTOINCREMENT`, so SQLite hands a freed rowid to the next account created.
  Either alone would hand passwordless access to a replacement account.
- **The guest account has no mail** (issue #816, §6.4). This is the one place
  the guest is treated as a guest, because a mailbox is not an area an account
  may or may not enter: it is the account's own correspondence. Every guest
  signs in as the same account, so its inbox would be read by strangers and
  anything sent from it would go out under one name many people type into.
  No level expresses that without also closing mail to every ordinary account
  at the guest's level. "The guest account" means the account guest login
  signs in without a password right now (`guest_is_eligible`); turning guest
  login off gives it its mail back.
- A guest session **may not manage the account's credentials.** The guest is an
  ordinary account in every other respect, but whether a session may touch an
  SSH key is a question about how that session authenticated, not about the
  account. The rule covers the whole key screen, not adding alone: a caller who
  proved nothing must not be able to mint a credential that outlives guest
  access being switched off, nor to strip the keys off a password-backed
  account -- removing the primary key changes the fingerprint its Link events
  are authored under. Signing in with the account's own password reaches key
  management normally.
- A guest session **may not change what other callers see of the account**
  (issue #1073). Every anonymous caller shares it, so whatever one guest
  writes there every later guest and every other caller gets: its bio and
  bio visibility, signature, display name, location, birthdate and their
  visibility switches, the verified-badge and Link-sharing switches, whether
  it takes direct messages, read receipts, whether its name is shown on
  Previous callers, who it blocks (from Profile or from Who's online), its
  MRC settings that the node-wide bridge reads per handle (private messages,
  last-seen, nick color), its chat alias (`/nick`, which is announced and kept
  in scrollback) and its MRC hub registration (`/mrc register`, `identify`,
  `roompass`, `update password`, and raw `/mrc send`, whose free text could
  carry any of them). Like the key screen, this is a check on how
  the session got in (`authenticated_without_credential`), not on the
  account: the entries stay where they are and say why when pressed, and the
  account signed in with its own password changes all of it as before.
  Birthdate matters beyond defacement: a self-entered birthdate is what local
  `min_age` gates check when the account has no age attestation (§18), so a
  guest who could set one would have opened every age gate for every later
  guest.
- A guest session's **display settings last for the call** (issue #1073). A
  guest on a plain-ASCII or 16-colour terminal needs a character set, colour
  depth, redraw style, banner speed, editor, colour toggles, sort orders and
  so on as much as anyone, so these stay open; but one guest's choice must not
  become the next guest's. For such a session every write to the per-user
  preference store and to the sort-order store stays in memory
  (`netbbs.user_preferences.session_scoped_preferences`, entered when the
  signed-in session starts) and its reads see its own choices over the
  account's stored values. The stored values are what every guest starts
  from; a SysOp sets them by signing in as the account with guest login off.
- A guest session's **drafts are its own and last for the call** (issue
  #1075). Post and file-description drafts are files named after the account,
  so on the shared account one guest's unfinished post was offered to the
  next, and two guests at once wrote the same file. A guest call keeps its
  drafts in a directory of its own (`netbbs.guest_call`, entered with the
  preference overlay), deleted when the call ends. Kept as files rather than
  in memory because that is what both editors write: autosave, `/exit` and a
  refused save's kept draft all work as for anyone else, within the call.
- A guest session **may edit or withdraw only what it wrote during the call**
  (issue #1075). Every guest's posts and uploads carry the one account as
  author, so "your own post" meant every guest's: any guest could rewrite or
  withdraw an earlier guest's words, and the edit was carried over the Link
  as the author's. The session records the posts (by root post id) and
  uploads (Zmodem or a web link) it creates; editing, withdrawing or
  describing anything else the account wrote is refused before an editor
  opens, with the reason on screen. Nothing else lets an author change their
  own content -- deleting a post or file is a moderator's, and mail is closed
  to the guest.
- A guest session **plays the bundled doors without a save that outlives the
  call** (issue #1075). Voidrunner and War Dialer key their saves on the drop
  file's `user_id`, so every guest shared one career and one crew, and the
  callsign a guest typed showed in Voidrunner's public Hall of Fame. A guest
  call plays a bundled door under a `user_id` of its own (above any rowid, so
  never an account's) with `VOIDRUNNER_SAVE_DIR` and `WAR_DIALER_DB_PATH` in
  the call's directory: Voidrunner starts from a copy of the node's Hall of
  Fame records and War Dialer from a copy of the world (SQLite's backup API),
  so the guest sees the real standings and rivals, and nothing it does
  reaches them. The door's outbound hook answers it as a rehearsal. The
  call's save stays for the rest of the call, so a guest can leave a game and
  come back. A SysOp's own doors keep receiving the guest account: what an
  external game stores is its own, a fresh identity every call would fill a
  legacy door's player list, and whether a guest reaches the door at all is
  its level.
- **The guest account holds no privilege a SysOp grants a person** (issue
  #1075): staff permissions, identity verification, or a moderator grant
  (edit, delete or approve on a board or file area; any channel permission).
  Each would belong to every anonymous caller at once. Granting one to the
  designated account is refused where it is written, with the reason; an
  account holding one cannot be designated; and `guest_is_eligible` refuses
  it at sign-in, for a grant made before either check existed. Read and post
  grants are not privileges in this sense: they open an area to the guest the
  way its level does, which is the mechanism this section describes.
- The re-checks are applied to the row that is **ultimately returned**, not
  only to the one first resolved. The login path awaits transport I/O and then
  re-reads the account to stamp `last_login_at`; a promotion, a block or a
  deletion landing in that window would otherwise have been read too early to
  matter. A deletion is a refusal like any other, not an error.

A SysOp account may not be designated. Everything else about guest access is
policy the SysOp chooses, but a passwordless SysOp login is not a choice worth
offering.

The **pre-login notice** is a short SysOp-authored line shown above the
sign-in screen -- the place a caller learns the guest account exists at all.
Unlike the welcome banner, which is authored ANSI art placed on the node's
filesystem and deliberately neither sanitized nor wrapped, the notice is typed
in the BBS and goes through the ordinary text path. It reaches Telnet and web
callers only: SSH has proven identity before there is any pre-login moment to
use.


---

## 5. Authorization, moderation, and identity attestation

### 5.1 Resource gates

Boards, file areas, channels, and Communities may apply:

- minimum user level;
- minimum age;
- verified-name requirements;
- visibility and membership policy appropriate to the resource type.

Resource-level scalar settings are nullable:

- `NULL` means inherit the containing Community’s default, if any, otherwise
  use the system default;
- an explicit value, including `0` or `none`, overrides inheritance.

A Community default is a default, not a mandatory floor or ceiling. A child
resource may currently loosen or tighten it explicitly.

### 5.2 Moderator authority

Boards and file areas distinguish read and write access. Channels use a join/
participation gate rather than asynchronous read/write separation.

Moderator permissions are composable primitives such as read, write, edit,
delete, approve, manage members, mute, ban, and topic control as appropriate.
Moderators need not be SysOps.

Authority scopes are:

1. per-object;
2. Community-blanket, applying to present and future matching resources in one
   Community;
3. local-blanket, applying to local-only matching resources on one node;
4. Link-blanket, applying to linked matching resources carried by a node.

Link-blanket authority does not imply local authority. A person who needs both
must receive both explicitly.

A board or file area's read or write grant lets its holder past that
resource's minimum read or write level (§5.1); the age and verified-name gates
still apply. This is how a SysOp lets a helper post on a board whose write
level is 255, such as an announcements board, without making the helper a
SysOp. A grant never lets anyone past a gate on a resource it does not cover.

Anyone holding an approve grant is told so: the main menu shows a
`Moderation (n)` entry with the number of posts and uploads waiting in their
scope, and it leads to one queue across every resource the grant covers, not
a visit to each board. A SysOp can grant moderation of every local board,
file area and channel as one action, written as the three local-blanket grants
in one transaction.

Only a SysOp can grant or revoke moderator grants of any scope, blanket or
per-object, or change node configuration. A suitably authorized Link-blanket moderator may initiate a new
linked resource, but:

- the node identity signs and owns the genesis event;
- the initiating human is recorded separately for audit;
- initiation grants no power to appoint further blanket moderators or alter
  unrelated resources.

Every moderation action is audited. Moderator changes to immutable Link content
must be represented as new authorized events, never silent mutation.

### 5.3 Board and file moderation

A board or file area may require approval before new posts/uploads become
visible. Local maintenance follows:

`active -> expired -> deleted`

with a grace period between expiration and deletion. Local pruning never
becomes a network-wide deletion instruction.

A revision ages from the later of its own `created_at` and its post's first
revision's (issue #793). This applies to every revision: edits, withdrawals and
moderator edits.
- **Why not its own alone:** a revision carried over the Link is stamped by its
  author's clock, display metadata that may run far behind (§7.2). Aged by its
  own stamp, an edit from a node with a badly wrong clock expired on arrival,
  and the post fell back to the revision before it. For a withdrawal, that
  re-showed the withdrawn text. Now an edit can never expire before its post.
- **A genuine later edit still keeps a post alive,** as before: its own stamp
  is the later one.
- **If the first revision was also stamped by the bad clock,** the post and its
  edits expire together, which is the accepted cost.
- **Receipt time was rejected:** ageing carried content by when this node
  received it would keep old history, carried late to a newly subscribing
  node, alive for a full maximum age.
- **Where it applies:** the sweep's expire and delete steps, the read-only
  listed count and the board rankings. A file has no revisions and ages by its
  own `created_at`.

**Pinning and keeping** (issue #675). A moderator with the board's or area's
edit permission can pin a post or file, and can keep it from expiring:
- **Where:** from the post reader (`P[i]n`, `[K]eep`) or the file area screen,
  on any approved post or file, not only one awaiting approval.
- **What a pin does:** a pinned post or file is listed first on the page a
  board or area opens on, marked "pin", in at most half the page's rows. It
  also stays in the dated listing where it was posted. So a pin the block
  has no room for is still reached by paging, and the opening page leaves
  out only the dated rows its block already shows. A page reached by paging
  or by a `[N]ew scan`/`[/] Find` jump has no pinned block, so a jump opens on
  its target.
- **How the block looks:** it sits under a labelled "Pinned" rule, parted
  from the dated rows by a plain one. On a board the labelled rule replaces
  the list's top rule, so the block costs the page one row, reserved only
  when there is a block.
- **Who sees pins:** listing a board's posts, pinned ones included, checks
  that the caller may read the board. That means the effective read level
  through the Community cascade and the minimum age, the two gates on
  entering. The name requirement gates posting, not reading, so it is not
  checked here. The trust filter (§12.8) applies to pins as to every row.
- **When `[K]eep` is offered:** only where content expires (the board or area
  has a maximum age), or where something is already kept.
- **Both flags belong to the post, not to one revision:** an edit keeps them,
  and a kept post's edit does not expire out from under it. Removing a post
  clears both.
- **Local only:** a pin is this node's own presentation and is never carried
  over the Link. A carried board's moderator pins for this node's callers.

**The author is told** (issue #678). Until a moderator decides, the author
sees their held post in the board's list, in its dated place, marked `held`
and dimmed; nobody else does. It opens read only, badged "awaiting
approval". A post with an edit of theirs held is marked `held` too, and its
reader says the edit awaits approval while it shows the current text. When
a moderator approves or rejects a held post or edit, its local author is
told:
- **once, at the main menu:** a one-line notice (`moderation_notices`), for
  example `Your post "X" on general was rejected: off topic`;
- **by mail, for a rejection:** from the system (§6.4, issue #819), with the
  reason, the moderator who decided, and the rejected text, since a
  rejection deletes the post and the author would otherwise have lost what
  they wrote. It used to come from the moderator's own account, so Reply
  wrote to the moderator personally about their decision.

Rejecting asks for a reason, which is optional (Enter leaves it out). It is
kept with the rejection record (§9.3's `post_rejections`) and goes to the
author. A carried post's author is on another node and is told nothing here,
and a moderator deciding on their own post isn't told either.

**The moderator's queue** (issue #678). Each board's and file area's queue
lists what waits with its kind (`post`, `reply`, `edit` or `file`), its
author and when it was submitted. A held reply names the post it answers; a
held edit is shown against the current text it would replace, subject
included. The SysOp console's content menu has one node-wide
`[P]ending review` queue of every held post and upload, oldest first, so
nobody has to open each board and area to find what waits.

A caller granted APPROVE on a board or file area has the same queue on its
own page: `[Q]ueue (N)` appears there while anything waits, and opens the
same decision screens without the pin and exempt keys unless they also hold
EDIT. APPROVE covers the whole
decision: it lets its holder reject a held post or upload as well as publish
it. Deleting something already published still takes DELETE.

A board's and a file area's detail screen in the console has `[H]istory`:
its moderators, meaning each grant that applies there, its own and blanket
ones, with their permissions, and under them what moderators did there,
newest first, from the moderation log (bounded, like the node's audit log).
The moderators are listed there, not on the detail screen itself, which at
80x24 has no row to spare.

**Expiry is a caller-facing boundary, not only a delisting** (issue #639).
Once a post or a file is `expired`, no keystroke a caller can press reaches
it: listings, `[/] Find` and the file area's own screens are
approved-and-current only, and knowing an exact name buys nothing. This
holds for both boards and file areas, and it is the whole of what a caller
may rely on.

Two consumers inside the node still resolve an expired row, and neither is a
caller-facing surface:

- **Reply-parent resolution.** `get_post` is deliberately unfiltered so a
  reply to a thread that expired mid-conversation still finds its parent
  (§6.1's edit chains depend on the same lookup).
- **SysOp recovery.** A file's bytes survive in content-addressed storage
  until the grace period ends. The file area's admin detail screen lists its
  expired files under `E[x]pired files`, oldest first, each with its purge
  date and `[D]ownload`: the same action the pending-file review screen
  carries, offered under the same transport rule (issue #475), so recovering
  an upload does not need shell access. The listing requires `APPROVE` on the
  area; the uploader has no view of their own, since an expired file is gone
  for them as for every other caller. Recovery is the whole of the screen:
  putting a file back in the listing is not an action, and a re-upload is how
  a SysOp who wants it listed again says so. Posts have no equivalent screen:
  a post's content is its text, and the recovery case that justifies the file
  screen does not arise.

A browser transfer link (§6.2) follows the same boundary. A download link
minted while a file was listed is refused once the file expires, its own
uploader included, and serves an expired file only to a holder of `APPROVE`
on the area, which is who the recovery screen mints it for.

A domain function returning an expired row is therefore a statement about
the domain, not a promise to callers, and the contracts of `list_files_page`,
`get_file`, `list_posts_page` and `get_post` say so in those terms. There is
no by-name file lookup: `get_file_by_name` had no caller once
`/download <filename>` was gone, and the recovery screen hands over the row
the SysOp picked rather than a name, which is not unique within an area.

### 5.4 Channel visibility and membership

Channel visibility and join policy are separate:

- listed or hidden;
- open to otherwise eligible users or members-only.

`hidden + open` is permitted but is obscurity, not access control.

Local invitations may be immediate for online users and retained with expiry
for offline users. Membership persists until revoked unless a channel defines
otherwise. Linked-channel membership eventually becomes signed governance;
it is not represented as end-to-end confidential from participating node
operators.

### 5.5 Identity attestation

Age and verified-name policy is local and jurisdiction-specific. NetBBS provides
mechanism, not a universal legal definition.

Users may provide nullable, independently visible:

- birthdate;
- display name;
- location;
- other profile fields.

Age is computed from birthdate at check time. It is never stored as a derived
current age. If a resource has an age gate and no usable birthdate or verified
age attestation exists, access fails closed.

A minimum age can also say how the age must be known (issue #1082). The age
requirement is:

- `none`, the default: a verified age attestation decides when the account
  has one, and otherwise the birthdate the caller entered;
- `verified`: only a verified age attestation counts.

It is stored and inherited exactly like the name requirement: a nullable
`age_requirement` on boards, file areas and channels, where `NULL` inherits the
Community's `default_age_requirement`, and a node-wide setting for MRC open
rooms. It means nothing without a minimum age. A verified attestation always
decides when there is one, so a verified 15-year-old is not lifted past 18 by
an older birthdate typed into the profile.

Against a `verified` requirement a caller is in one of three states. With a
verified age old enough, they pass. Old enough only by the birthdate they
entered, they are **unverified**: the resource is still listed for them, marked
"needs verification" as an unmet name requirement is, and entering it refuses
with what to do ("Ask the SysOp to verify yours"; the Staff list names who).
Too young, or with no usable birthdate, the gate hides the resource as any age
gate does. Nobody bypasses it, level 255 included, the same as the name
requirement. A remote author is always held to a verified age, since a remote
node's self-entered birthdate never reaches this one.

A `user_attestation` records:

- subject user;
- attribute (`age` or `name`);
- attested value;
- verifier identity;
- signature;
- creation time;
- Link-visibility preference.

A verifier may use a personal key, or the node may vouch for a password-only
verifier. Verified values take precedence over self-reported values.

`can_verify_identity` is a separate SysOp-granted boolean, not another content
moderator tier.

Name requirements are:

- `none`;
- `verified`, requiring a verified name without compulsory display;
- `verified_and_displayed`, requiring resource-scoped visible disclosure.

A verified name never overwrites the user’s self-chosen display name. When a
resource requires display, render:

`display_name_or_username (=Verified Real Name=)`

The complete `(=...=)` unit uses a dedicated trusted color. The `=` marker
remains visible when color is stripped or inaccessible. User-controlled display
names may not contain the reserved `=` marker, and untrusted text cannot inject
ANSI styling.

Disclosure is resource-scoped. A resource which requires visible identity must
not cause the real name to leak into unrelated screens.

Remote propagation of attestations requires:

- the subject’s explicit opt-in;
- the issuing node's SysOp having named the receiving node as a recipient;
- Phase-4 trust rules allowing the receiving node to decide whether to trust the
  remote verifier.

Link visibility is per attested attribute, independent of profile-field and
verified-badge visibility, and defaults off. Re-verifying an attribute resets
its Link visibility to off so consent for an old value never silently carries
onto its replacement. A node exports only a currently Link-visible local
attestation and signs a `remote_identity_attestation` object containing its
stable issuer fingerprint, the subject's `node_vouched_user` identity pair,
attribute/value, explicit opt-in assertion, issuance time, and expiry. The
maximum active lifetime is 365 days. Revocation is a separate signed
`remote_identity_attestation_revocation` object naming the exact original
content ID. Expiry and revocation both retire an attestation, and a retired
attestation has the columns that carry its value blanked (the stored value,
the envelope and the signature) while its row stays, on the issuer and on
every receiver running this software (issue #596).

Issuance is reconciled once per sync pass against current local consent, not
performed by the screen that records it: the signature must be made by the
node's current operational key, and a caller session has no business holding
one. Each pass signs an object for every Link-visible attestation without a
live one, re-issues one approaching expiry, and signs a revocation whenever the
consent behind a live object has gone — the toggle switched off, the
attestation removed, the value re-verified, or the account deleted. A deleted
account's signed objects therefore outlive the account itself long enough to be
revoked, rather than leaving subscribers holding a live assertion about a user
who no longer exists.

An object is also reissued when the node rotates its operational signing key,
without waiting for the renewal window: a subscriber resolves only the issuer's
current key, so anything the previous one signed stops verifying at the moment
of rotation. A revocation whose target has not expired is signed again under
the new key for the same reason (issue #623); a subscriber that already holds
the first treats the second as a repeat.

Attestation ingress is bounded by total retained volume as well as by rate.
The page and per-pass limits bound how fast a configured authority can deliver
objects; a per-issuer cap on active attestations bounds how many it can
accumulate, so an authority that turns hostile cannot grow a receiver's
database without limit.

Issued lifetime is 90 days, renewed once 30 days remain. The 365-day ceiling is
what a receiver must tolerate; a shorter issued lifetime is what an issuer
chooses, because a node that goes dark cannot withdraw consent it has already
published, and the issued lifetime is the window in which an opt-out that never
reached a subscriber still leaves a live assertion standing. Overlapping the
renewal with the object it replaces is deliberate: a receiver selects the
newest unexpired record, so the handover needs no revocation, and revoking
would tell a subscriber to stop trusting a value being re-asserted in the same
breath.

Propagation is a sealed, signed snapshot per recipient (issue #632, §16): the
issuer pushes each recipient node everything it should currently hold, sealed
to that node's key, directly or through the recipient's relays (§8.5), and a
snapshot is authoritative -- what it leaves out the recipient forgets. A
snapshot holds every live attestation and every revocation whose target could
still be held; once the target has expired, absence says the same. A recipient
uses a snapshot only from an issuer it names as an attestation authority,
never from a trust reporter alone, and only objects that issuer signed about
its own users. There is no pull (issue #1046): a recipient whose descriptor
does not advertise `sealed_attestations` receives nothing until it upgrades.

What reaches whom is the issuer's decision, and separate from every decision
the receiver makes. A node sends its attestations only to the nodes its SysOp
has named as recipients, a list that starts empty, is seeded from nothing, and
is per node rather than per attribute: the caller's two toggles already decide
which attributes leave at all. A node removed from the list is sent an empty
snapshot and forgets what it held (issue #632).

A receiver forgets what it is told is withdrawn. Ingesting a revocation blanks
the stored value and envelope of the attestation it names, and each sync pass
does the same for attestations that have expired. The rows stay, because the
revocation, the effective projection and the audit trail reference them, and
because a retained content ID makes a re-offered copy of the same object a
no-op. This is the whole of what opting out can promise: the issuer stops
serving the value and removes its signed copy from its live database, a
receiver running NetBBS does the same, and nothing forces a node that copied
it to. Removal is from the live, served database and is not forensic erasure:
the write-ahead log until its next checkpoint, freed pages, and backups taken
while the value was shared can still hold the bytes.

A SysOp can see everything their node currently asserts about its own users,
which nodes any of it is given to, and stop any of it. The same screen holds
the recipient list and says so plainly when that list is empty, since
published objects beside no recipients publish nothing, and it lists each
recipient with how it was last reached, when, and whether that delivered the
current snapshot, or why the last attempt failed (issue #632). A caller sees
how many recipients hold their own value beside their own sharing toggle --
"sent to 2 nodes", "sent to 1 of 2 nodes", "not delivered yet" -- never which. The listing
names the subject, the attribute, and the expiry, but never the attested
value: it is a screen about what leaves the node, not a place a verified real
name belongs. Withdrawal clears the
subject's consent and lets the ordinary reconcile sign the revocation, so the
operator action and the sync pass can never disagree about whether an object
should exist. There is deliberately no operator way to switch sharing *on*:
propagation is conditional on the subject's own opt-in, and a SysOp who
should not be asserting something can stop asserting it or withdraw the
verification itself, neither of which requires speaking for the caller.

Receiving nodes verify canonical bytes with the issuer's currently authorized
operational signing key before persistence. Acceptance then remains purely
local and attribute-scoped: an explicit attestation-authority grant, its
`age`/`name` scope, the issuer node's current identity-integrity trust state,
expiry/revocation, and an optional reasoned SysOp accept/reject override produce
a persisted effective projection. Reporter/vouch configuration grants no
attestation authority. A manual accept may select a current valid signed record
from an otherwise unconfigured issuer, but cannot resurrect an expired or
revoked record. Ordinary callers see only whether the gate is met; issuer
configuration, notes, and override reasons remain SysOp-only.

Remote real-name rendering uses the same trusted `(=...=)` unit as local
attestations and only within a resource requiring
`verified_and_displayed`; accepting a remote attestation never exposes that
value in unrelated screens.

A carrying node may always apply its own local attestations to its own users
when enforcing a carried resource’s local age/name policy.

For a remote author the same gate consults the accepted remote attestation,
resolved through the Community cascade exactly as the local posting path
resolves it, at the point §12.8 puts every Link enforcement decision: before
remotely influenced persistence. A carried board post whose author fails the board's own
`min_age`/`name_requirement` is not materialized, which is the same honest
exclusion as a board this node does not carry — the signed event is retained,
nothing is projected, and the SysOp's rebuild pass materializes the post if the
attestation arrives later. The refusal is silent on the wire: telling the
network which of its users fail a local gate would disclose exactly the policy
§12.8 keeps undisclosed.

### 5.6 Staff permissions, the Staff list, and the away notice (issue #836)

A SysOp can share the node's day-to-day work without handing over the node.
**Staff permissions** are account-wide grants a SysOp gives to an account
below level 255, independent of its level:

- **Approve accounts:** approve or decline registrations waiting under
  `approval_required` (§4.2).
- **Manage accounts:** disable an account and enable it again, reset its
  password, and set its level anywhere from 0 to 254. Raising an account to
  255, and deleting an account and so retiring its name (§4.3), stay with the
  SysOp.
- **Moderate everything:** act as moderator on every board, file area and
  channel on the node, local and carried, with every moderator permission of
  §5.2. Without it, a staff member moderates what their moderator grants
  cover and nothing more.

The verify-identity permission (§5.5) is shown and granted beside them, but it
remains its own grant: it is about attestation, not about running the node.

**Co-SysOp** is a preset, not a role. On an account's detail a SysOp can apply
it in one confirmed step: it sets all three staff permissions. Afterwards the
account holds exactly those permissions, and the SysOp can remove any of them
one at a time. The account detail's privileges group lists the staff
permissions, the verify-identity permission and a summary of the account's
moderator grants, so a SysOp sees everything an account may do in one place.

**What staff can never do.** A staff member acts only on accounts below level
255 that hold no staff permission; a moderator-only account is within reach.
They cannot set any level to 255, grant or revoke staff permissions or
moderator grants, or reach Settings, Link, node controls, managed DNS or
backups. Every action they take is audited under their own name. So the
original SysOp cannot be demoted, disabled or locked out by a helper, and the
usable-SysOp invariant (§4.3) is never at stake in a staff action.

**The Staff console.** A staff member reaches the same `[S]` entry on the main
menu, labelled for them `[S]taff`. It opens a reduced console: a landing view
of its own, which holds the away notice below, and only the screens their
permissions reach: the accounts waiting for approval and the
account list for the account permissions, and the node-wide moderation queue
filtered to what they moderate. The screens are the SysOp console's own, not
copies, so they cannot drift apart. The SysOp's console is unchanged.

**Who is told.** The notice that accounts are waiting for approval goes to
usable SysOps and to holders of the approve-accounts permission, and to no one
else.

**The Staff list.** Every member can open a list of who runs the node: the
usable SysOps, the staff members, and the moderators with what they look
after, in the words of the account detail's grant summary. Each row shows the
date of the person's last session, not the time. A person on the list is
someone members are meant to find, so the Previous callers privacy choice does
not hide them here; the confirmation that makes someone staff or a moderator
says so. Guests and pending accounts do not see the list.

**The away notice.** A SysOp or staff member can mark themselves away, from
their console's landing view, with a message of one short line of plain
text (no pipe codes) and an optional return date. It is per person, not per
node. It shows:

- on the Staff list, beside that person;
- in the message a pending account sees at login and just after it
  registers, when every account that could approve it is away: then the
  message names the approver expected back first, their return date if any,
  and their message.

Being away changes nobody's permissions. Logging in does not end it, since a
SysOp who is away may still look in. The person ends it, or, when it has a
return date, it ends by itself once that date has passed. A notice without a
date stays until it is ended, so it never claims more than it says: wherever
it is shown it reads "away since" the day it was set, and the person's own
console landing view shows it to them each time they log in, as a reminder
to end it.

Staff permissions, the Staff list and the away notice are local to the node.
None of them is carried over Link: a staff member's moderation of carried
content follows §5.2 and §9.5 exactly as a moderator's does.

### 5.7 The access map (issue #1004)

Level gates are set in many places: on each board, file area, channel and
door, as Community defaults, and in node settings. The access map is the one
list of all of them, so a SysOp can see what a level opens and what a level
change gains or loses without opening every resource. The SysOp's level
screens (#1006-#1009) show it; this section says what it contains.

**What is on it.** One gate per thing a level opens:

- each board and file area twice, for reading and for posting or uploading;
- each chat channel (entering it) and each door (playing it);
- the node-wide gates: the node map, mail, opening new MRC rooms, and the
  SysOp console at 255.

Resources excluded from this node (§9.5) are left out, since nobody reaches
them.

**Each gate says where its level comes from:** set on the resource, inherited
from its Community's default, the system default of 0, a node setting, or
fixed (the SysOp level).

**The level a gate opens at.** Usually its own level. Posting on a board or
uploading to an area also needs its read level, since a caller who cannot
open the board cannot post on it, so a write gate opens at the higher of the
two.

**Levels only.** The map answers what a level opens. It does not claim more:

- Age, verified-name, members-only and hidden are facts about each account
  or each channel's membership. The map names them as the gate's conditions,
  such as "age 18+", rather than counting them in.
- A read or write grant (§5.2) lets one account past one resource's level.
  It belongs to that account, not to a level, so the map does not list it.
- A gate that opens for nobody right now says why: a closed board, Link off
  for the node map, MRC open rooms switched off.
- A carried Linked board's write level holds only this node's callers: posts
  carried in from other nodes are not held to it (issue #993). The map says
  so on that gate.

**The Levels screen, LAST** (issue #1007). `Users ▸ [L]evels (LAST)` opens the
Level Admin SysOp Tool, which lists the levels that
matter: 0, every level a gate opens at (switched-off gates aside), and every
level an enabled, approved account holds. Each row shows how many such
accounts hold exactly that level and what it first opens. A level's own
screen lists its gates with their level and its source, in four views: new
at this level, everything open, still closed, and open with another gate
still applying. Picking a board, file area, channel or door opens the screen
the Content menu opens for it. Any level, in use or not, can be looked at.

**Level fields say what they mean** (issue #1008). Every level field in an
editor shows, next to its value, how many enabled, approved accounts are at
that level or above, recounted from the draft as the SysOp types:

- on a board or file area, a level left to inherit shows the level it
  inherits and from where ("none: 10 from Community Market");
- a write level below the read level is counted at the read level, since
  posting needs reading, and says so;
- a Community's default read and write levels also show how many boards and
  file areas inherit them;
- channels, doors and the node settings (node map, mail, MRC open rooms) show
  the count alone.

The count is by level only; age and verified-name gates depend on each
account and are not counted in.

**Level names** (issue #1009). A SysOp can name any level from 0 to 254, such
as "Member" for 10, from the Levels screen; 255 is always "SysOp". A name is a
label: gates and accounts keep their numbers, and naming, renaming or clearing
a level changes nobody's access. The console shows a named level as
`10 (Member)` wherever it shows a level: the Levels screens, the change
preview, the user screen and the editors' level fields. The user screen's
level prompt and `[G]o to level` take a level's name as well as its number.
A name is at most 12 characters, needs a letter (so it cannot read as a
number), and is unique regardless of case. Names are node configuration and
travel in backups. Naming is recorded in the moderation log.

**From the shell.** `python -m netbbs.admin last` (`levels` also works) prints the ladder;
`levels <level>` (a number or a name) prints what a level opens and what stays
closed; `levels --user <name> --to <level>` prints the change preview for one
account. Each takes `--json`. The command only reads.

**A level change is previewed for the account** (issue #1006). Changing an
account's level from its detail screen shows what the change gains, loses and
leaves blocked for that account before anything is written. Unlike the map
itself, the preview is about one account: it counts that account's read and
write grants, so a grant that keeps a board open is no loss, and it moves
whatever another gate still keeps the account out of (age, verified name, a
members-only channel it is not in, mail for the guest account) into its own
"still blocked" list instead of the gains. Something the account was kept out
of anyway is not shown as a loss. The screen's `[A]pply` makes the change and
`[B]ack` leaves the level as it was. A change that would be refused (the last
SysOp, a level outside 0-255, a staff member raising someone to 255) is
refused before the preview, and a change that opens and closes nothing is
applied at once, its outcome line saying so.

**It agrees with the checks.** The map is built from the same effective-level
functions the checks use, and the test suite holds each gate's answer to the
real check for accounts at every threshold. Every level check in the code is
either on the map or recorded as not a gate, so a new gate cannot be left off.

---

## 6. Local product domains

### 6.1 Message boards

Local boards provide:

- categories and stable navigation IDs;
- posts and replies;
- moderation and pending approval;
- expiry, pinning, and exemption;
- immutable revision history for edits;
- simple and fullscreen composition.

Read/unread state, follows, activity discovery, and local search across
boards, file areas, channels, and Communities are specified together in
§6.6, not per-domain.

A visible edit is a revision, not destructive replacement of history. Any
threading or revision semantics which affect Link event IDs or propagation must
be settled in Phase 3; only presentation refinements may wait until Phase 7.

**A board is a list of posts; a post is read on its own screen** (issue #679).

The list:
- Shows one row per post: number, subject, a `new` marker, author and date.
- Fits as many rows as the terminal holds and pages with
  `[O]lder`/`[N]ewer`/`[R]ecent`.
- Has a cursor: Up/Down, and Enter or a digit to open a post.
- Follows §3.6: display-width columns. Below readable width, a row becomes
  "subject -- author". Author gives way to subject first, because the reader
  shows the author in full.
- Has a header that says where the caller is and what they can do: the path
  they came through (Community, "Message boards", category), newest or older
  posts, how many were new on arrival, and "Linked" or "linked from X".
- Shows the board's description and, for a caller who can read but not post,
  why ("Read only: posting needs level N", or a name that needs
  verification).
- Marks a post `new` until the caller opens it (§6.6, issue #710): showing
  a post in the list does not count as reading it. `[M]ark all read` counts
  everything on the board as read; it is offered only while something is
  unread.
- Opens a `[N]ew scan` or `[/] Find` jump with the cursor on its target.

A post opens on `show_detail`:
- The title, a byline (author, date, `edited`, `new`, the post it replies to)
  and the action bar stay on screen while a long body pages with PgUp/PgDn.
- The post's own actions live there, offered only when they would succeed:
  `[R]eply`, `[H]istory`, `[E]dit`, `[W]ithdraw`, `Remove pos[t]`,
  `P[i]n`/`[K]eep` (§5.3), and `[N]ext post`/`[P]revious post`, which cross
  page boundaries.
- `[B]ack` returns to the list with the cursor on the post last read.

**Revision history** (issue #675). `[H]istory` lists a post's versions,
newest first. Each opens read-only on the same reader. A version is marked
"current", "original", "edit" or "withdrawn", plus "by a moderator" where one
wrote it.
- **Who:** moderators only, meaning holders of the board's edit permission,
  which a moderator edit needs. A reader, and the author, see no history,
  only the `edited` badge. The earlier rule, where readers saw back to the
  last moderator edit, was replaced. It depended on working out after the
  fact who made each edit, and deleted accounts made that unreliable in the
  direction that shows readers what a moderator edited away.
- **What moderators see:** every approved version, those of a removed or
  withdrawn post included. Expired and pending revisions are left out, and so
  is the removal placeholder.
- **Order:** the chain's own links (`edit_of_post_id`), not `created_at`,
  which for a carried revision is another node's clock (§7.2).
- **Limit:** at most the 50 most recent versions, because a carried post's
  chain is written by another node.
- **Expiry:** swept before listing.

**Withdrawal** (issue #675). The author's `[W]ithdraw` replaces the post's
text with "[withdrawn by author]" and keeps its subject, so replies still read
as answers to something.
- **Not final.** It is a revision the author may edit past later, not a
  removal.
- **Hidden, not deleted.** A moderator can still read the withdrawn text in
  the history, and every node keeps the signed original. The confirmation
  says so.
- **Not held for moderation.** It only takes text away. Held, a moderated
  board would go on showing what its author took back until a moderator got
  to it.
- **Pin and keep.** It clears both, as a removal does.
- **On the Link:** a `board_post_edit` with `"withdrawn": true` (§16). A
  carrying node that knows the field applies it the same way, without local
  moderation or a trust hold.

**Replying** (issue #675). A reply is a post with the replied-to post as its
parent, listed on the board like any other post. There is no threaded view.
- **Who:** `[R]eply` is offered to anyone who may post on the board, on any
  post that has not been removed.
- **Subject:** starts as "Re: <subject>". A subject that already starts with
  "Re:" gets no second prefix, and the prefix never pushes a subject over its
  limit.
- **Body:** starts as the post quoted, with the cursor under the quote:
  "<author> wrote:", then each line of the post before its signature with
  `> ` in front. A line that was already quoted becomes `> > `.
- **Attribution line:** "<author> wrote:" is always a line of its own. The
  board reader used to reflow prose, and joined the text a replier wrote
  straight under a trimmed quote onto it, so their words read as the quoted
  author's (#837). Posts now keep their lines (see **How a post reads**),
  which keeps the attribution apart for any author, as mail always did.
- **How a post reads** (issue #837): a board post keeps the lines its author
  wrote, as a letter does (issue #809): the reader, the review screen, the
  pending-post screen and a post's history show every line as written and
  wrap only a line wider than the terminal, at a word
  (`netbbs.rendering.post_body.lined_body_rows`), in every display mode and
  for posts carried over Link alike. Posts used to be reflowed into
  paragraphs, which merged bullet lists, sign-offs ("73, Harold") and short
  separate lines into one, in both editors' output. The line editor's blank
  line is a paragraph break and the fullscreen editor's lines are the
  author's, so neither needs a reader to rejoin lines. A post written before
  this that relied on reflow -- a paragraph typed as several short lines --
  shows those lines as typed. Rejected: reflowing only posts from the line
  editor (the reader cannot tell the editors apart, and a carried post has
  no editor at all) and a per-post "keep lines" flag (a second layout for
  the same text).
- **Files in a post** (issue #842, F086): a post points at files in this
  node's file areas exactly as a letter does (**Files in a letter**, issue
  #830), reusing `netbbs.file_refs` and `netbbs.net.file_ref_view`.
  - *Stored per revision:* `post_file_refs` rows keyed by the revision's
    content-addressed `posts.post_id`, so an edit can attach or remove a file
    and a held edit's files wait with it. `edit_post` keeps the current
    revision's files unless it is given a list: a moderator's edit keeps
    them, a withdrawal and a tombstone drop them. A file newly attached must
    be one the writer can open; one already on the post stays, shown as no
    longer available if it has gone. The reader and the edit take the files
    of the newest approved revision (`shown_post_refs`), a held post its own.
  - *Readers:* the reader lists them under the byline and `[G]et file`
    downloads one after `open_ref` checks again. A reader who may not read
    the file's area sees "A file in a file area you can't open", no names.
    Unlike a letter, nothing is refused at Publish when some of the board's
    readers cannot open the area -- a board has no recipient list to check.
    The review screen says so instead (`refs_some_readers_cannot_open`:
    the area's effective read level or age is stricter than the board's).
    The moderation queue shows a held post's files, and a held edit's
    proposed and current files where they differ.
  - *Over Link:* no reference crosses. `board_post`, `board_post_edit` and
    `board_post_moderator_edit` events carry `carried_post_body`: the body
    with one `link_text_line` per file after a blank line, the same line as
    mail's. The review screen counts those lines toward the length limit on
    a Linked board. A carried post never gets rows, so a remote moderator's
    edit of a local post leaves the new revision with its files as text only.
  - *Removal:* no foreign keys, for mail's reason (`posts` is rebuilt by
    migrations too). Every hard delete of posts rows -- `delete_post`, the
    expiry sweep, `delete_board` and purging a hidden carried board -- calls
    `forget_orphaned_post_refs_without_commit`, which removes rows whose
    revision is gone.
  - A post draft keeps its text, not its files: board drafts are plain text
    files, unlike a letter's.
- **Quote limits:** a quote is at most 40 lines and 8 KB, and a cut quote
  ends with `> [...]`, so a reply to a long post stays writable in the line
  editor.
- **Color boards:** where color is allowed, the quote is the text a reader
  with color off sees.
- **Art posts:** an art post is answered without a quote.
- **Parent:** the reply's parent is the replied-to post's root, which every
  revision and every carrying node shares. The reader's byline names it, and
  `[N]ew scan`'s "replies to you" pass finds it.
- **Mail:** mail's Reply starts its body with the same quote and its subject
  with the same rule (`netbbs.quoting`).
- The mail message view and the SysOp's pending-post review use the same
  reader. Mail keeps its writer's lines (§6.4).

The board picker adds an activity column ("N new", "caught up", "not visited
yet", in §6.6's terms) and an "about" column that leads with `[LINK]` and a
name-gate note before the description.

**Color in posts** (issue #711). A board's SysOp may allow color in its
posts ("Color in posts", off by default). A carried board follows the
carrying node's setting. Authors color a body with Mystic pipe codes
(`|00`-`|15` foreground, `|16`-`|23` background). A body is stored and
carried exactly as written and filtered on output
(`netbbs.rendering.post_body`):
- **What survives:** text, line breaks, and SGR limited to foreground,
  background, bold, underline and blink. Every other escape sequence is
  removed whole: cursor movement, clears, mode changes, titles,
  OSC/DCS/APC/PM/SOS strings and their 8-bit forms, and the bell. A post can
  never clear the screen, move the cursor, hide text or fake a prompt.
- **Where:** bodies only. Subjects stay plain in lists, search and
  breadcrumbs, and search indexes a body's plain text.
- **Who sees what:** where color is allowed, a reader with "Post colors" on
  (Profile, on by default) sees it; with it off, plain text without the codes.
  Where it is not allowed, a body shows as text, pipe codes as typed.
- **Layout:** a colored body keeps its lines like any other. Every row
  restates the color it inherits and ends with a reset, because the reader
  pages by rows.
  The state is kept normalized, so a flood of codes costs each row one short
  prefix. The review screen and the SysOp's pending-post screen show a body
  the same way.
- **Art posts:** on a board that allows color, `[A]rt post` opens the ANSI art
  editor (`netbbs.net.ansi_editor`) on a canvas as wide as the terminal, up to
  80 columns, on a terminal of at least 40x12. A drawn post keeps its lines: each line stays a line, and only a
  line wider than the reader's terminal wraps, cut at the column, with its color
  carried over. The layout belongs to the post, set by the editor that drew it:
  editing an art post reopens the art editor, and a carried post brings its
  layout along (§9.2).
- **Pasted color** (issue #754): the post editors show color as pipe codes, so
  that is how pasted color arrives. On a board that allows color, the line and
  fullscreen prose editors type a pasted SGR (`ESC [ <digits;> m`) in at the
  cursor as the equivalent pipe codes. Bold becomes the bright foreground, a
  bright background its base color, and a return to the default foreground
  `|07`. Underline, blink, 256-color and truecolor have no pipe code and are
  dropped. The editors have no notation of their own for bold, underline or
  blink. The author sees and edits the codes as text, and the stored body stays
  in the one notation the editors show. On a board without color, and at every
  other prompt, a pasted SGR is dropped as before. A post with SGR of its own
  (carried over the Link, or written by another client) still shows it through
  the filter above.

### 6.2 File areas

Local file metadata lives in SQLite; file bytes use content-addressed filesystem
storage. Areas support permissions, moderation, expiry, and two transfer paths
(issue #475).

**Zmodem** on byte-capable transports, for terminals that implement it —
SyncTERM, NetRunner, Qodem, Tera Term, ZOC, MobaXterm, minicom. It is a protocol
*inside* the terminal stream, so the emulator must drive it; PuTTY, Windows
Terminal, an ordinary OpenSSH client and this project's own browser terminal
cannot, which for most callers meant no transfer at all.

NetBBS speaks Zmodem the way lrzsz does, because that is what terminals are
built against (issue #963; the first version only ever talked to itself and
no real client could complete a transfer). A download opens with `rz\r` and a
ZRQINIT in the hex header form, an upload with a hex ZRINIT: those are the
patterns a terminal's auto-start watches for. Every header read accepts hex,
CRC-16 and CRC-32 binary headers. ZCRCW ends a frame, and the next one opens
with a fresh ZDATA header. The sender streams when the receiver says it can
(CANFDX and CANOVIO) and honours ZRPOS at any point, which covers both resume
and error recovery; the receiver asks for data again after a CRC error or a
frame at the wrong offset, up to a fixed number of times. A transfer ends with
ZFIN both ways and `OO`, and what the terminal still sends is read off the line
so it can't arrive as keystrokes. Five Ctrl-X from the caller cancel; a
transfer NetBBS gives up on sends CAN×10, BS×10 so the terminal stops too.
Deliberately left out: sending with CRC-32 (not asked for, since TCP already
checks every byte), run-length encoding, compression, encryption, remote
commands, and more than one file per transfer (later files in a batch get
ZSKIP). `tests/test_zmodem_lrzsz.py` runs real `sz`/`rz` where lrzsz is
installed and is skipped otherwise.

**A session-bound HTTP link** for everyone else, served by the same aiohttp
application as the web terminal. The file screen adapts to the transport: Zmodem
where it can be carried, a link where it cannot, and `[W]eb transfer` for a
terminal whose own emulator lacks Zmodem. A caller already in the browser is
handed the transfer rather than the URL — the page opens a drop target or starts
the download itself.

A transfer grant is not a capability. It records who asked for what; every gate
the terminal path applies — level, age, name requirement, Community inheritance,
moderation state, maximum upload size — is applied again when the link is
redeemed, against live rows. It is single-use, expires in minutes, names its area
and file by content-addressed ids that no deletion can recycle, and lives in
memory only. A node that cannot say how it is reached (`[web] public_url` unset,
listener on a wildcard address) says so rather than printing a URL that fails.

`max_upload_bytes` bounds the *file*, identically over Zmodem and HTTP. An HTTP
upload as a whole may cost the node that plus a small fixed framing allowance,
whatever its shape: preamble, part headers and every part before or after the
file count, and a request past the bound is refused (issue #511).

A finished upload is reported both ways (issue #842). The terminal that minted
the link is told when the file is stored, and whether it waits for approval,
as an outcome on its next screen; Ctrl-L on the file list looks again, and the
browser page sends it once its upload is done. The HTTP answer is JSON for the
browser terminal's own `fetch()` and for scripts, and a short page for a form
submitted from a browser tab (a request that accepts `text/html`), which used
to be left on raw JSON. A failed Zmodem transfer leaves the caller on the file
list and names `[W]eb transfer` when the node can mint links.

A `HEAD` on a transfer link answers with the status a `GET` would, the reason in
`X-NetBBS-Transfer-Message`, and spends nothing. The browser page relies on it:
it probes a download first and starts it only on a yes, so a refused download is
shown to the caller rather than saved under the filename. The answer is advice,
not a reservation. The page addresses the link on its own origin, under the path
it was loaded from, so a `public_url` naming another origin or a reverse-proxy
prefix does not break the transfer (issue #511).

**A JavaScript Zmodem implementation for the browser terminal is not planned.**
It was listed as a possible follow-on while issue #475 was open, on the reasoning
that keeping one transfer protocol everywhere would be simpler than maintaining
two. What shipped answers the same need better: a browser caller gets a drop
target and a download the page performs itself, which is what their environment
is actually good at, and the bytes never pass through the terminal stream. A
Zmodem implementation in the page would add a second protocol to maintain, in
JavaScript, to reach exactly the callers already served — and it would put file
transfer back inside the byte stream it was moved out of. Zmodem remains the
right answer on terminals that already implement it, which is where it stays.

**SysOp uploads** (issue #728) reuse both routes to put one file at one fixed
place on the node: a banner or masthead piece's own file (**[U]pload** on its
screen), or a named file in the doors folder (**Content → Doors → [U]pload**).
The contract:

- The destination is chosen before any transfer is offered. The name the
  sending side reports is recorded but never decides where bytes land. A door
  file's name is a plain name: no folder part, no leading dot, no control
  characters or colon.
- Replacing an existing file is confirmed first. The consent is re-checked when
  the bytes arrive, and a file that has appeared since is not overwritten. A
  symlink or directory at the destination is refused, never followed. The
  replacement keeps the old file's permission bits.
- Caps: 256 KiB for banner art (what **[E]nable** accepts). For a door file, the
  node's `max_upload_bytes`, read live when the link is redeemed.
- The grant is redeemed like any other. Its account must still be an enabled,
  unblocked SysOp when the link is used and again once the body is in.
- The file is written beside the destination and renamed over it, so a failed
  upload leaves the previous file in place. Every upload that lands is
  audit-logged with its size and destination, even if the session that
  started it is torn down mid-install.
- An upload never enables a piece, registers a door, or sets an execute bit.
  Those stay separate, explicit steps (**[E]nable**, **[F]rom disk**).

This adds no trust boundary: a SysOp can already run any program on the host
through a door's **Test as SysOp**. It removes the need for a separate OS-level
account just to place a file.

File bytes are node-local. NetBBS Link will distribute catalogue/descriptor
information and fetch content on demand in bounded resumable chunks. It will
not replicate every file to every node.

Every file carries an optional description, and the two ways one is written
are deliberate (issue #463).

An uploaded archive is read for a `FILE_ID.DIZ` member, the BBS-era convention
where the archive's own author, not its uploader, supplies the catalogue text;
finding one fills the description in with no further interaction. ZIP is read
in-process and recognised by content rather than by the uploader's chosen
extension. Legacy formats (`.lzh`/`.lha`/`.arj`/`.rar`/`.7z`) are read through
whichever external unpacker the SysOp has installed, and installing one *is*
the opt-in — NetBBS never installs an unpacker, and a node without them simply
gets no description from those formats. Because every such tool parses hostile
input, extraction reads the member from the tool's stdout and never writes to
disk, passes argv lists with a fixed member name and no shell, applies POSIX
CPU/memory/process rlimits, bounds the read and the wall clock, and kills and
reaps on every exit path. No archive, no DIZ, no unpacker, or a corrupt member
are all "no description", never a failed upload.

Every description, whether extracted, typed, or carried in a peer's
`file_descriptor`, is stored as plain text. Whole ANSI escape sequences are
removed, not only their ESC byte, which would leave `[0m`-style remains from a
colour DIZ (issue #1000). Control and bidi-override characters are stripped
after that. A DIZ's SAUCE record, and anything after its DOS end-of-file byte,
is dropped before decoding. Colour in a DIZ is not kept: descriptions have no
colour markup.

A description may also be written by hand, from the file listing, by the
uploader themselves or by anyone holding `EDIT` on the area. Hand-written
edits are local-only: a `file_descriptor` (§11.2) is immutable and single-shot,
so a file whose descriptor has already been signed keeps the description its
peers were told about. In a Linked area that is most edits — an approved
upload's descriptor is signed and queued the moment it lands (§11.2) — so the
caller is told the change stays local rather than left to assume it travels.
The exceptions are files no descriptor was ever built for: one approved before
its area was promoted (pre-Link history is never backfilled), one in a carried
area, and one whose name or size no descriptor may carry. The interface asks
the file itself, not its area, before saying anything about what peers hold.

Either way a description is stored already normalized — control and bidi
characters stripped, at most 10 lines (the DIZ format's own limit, and what
keeps a listing page bounded), and within the same byte ceiling §11.2's
`file_descriptor` accepts, so a locally valid description is always one Link
can carry. The area listing renders every line of it, because a DIZ's line
layout is content.

### 6.3 Real-time chat

Local chat is typed event traffic, not preformatted strings. Initial event types
include:

- ordinary message;
- `/me` action;
- online private message;
- join/leave;
- alias change;
- system notice.

An optional `/nick` alias is presentation metadata only. Every context retains
the authenticated canonical identity, and permissions, moderation, blocking,
reputation, and addressing always use canonical identity.

An alias is always shown with the username beside it, as `alias (username)`,
in the live stream as in `/who`, `/whois` and `/names` (issue #843). In the
live stream the alias leads in its own color and `(username)` follows muted
(issue #899): the username is there so no alias stands alone, not to compete
with the name its owner chose. It may not contain `( ) | [ ] < > * ~`: the
parentheses that hold the account, a status-bar tag's brackets, the angle
brackets around a speaker, the `*` of actions and notices, and the two earlier
alias markers -- nor any character that reads as one of these (one that
compatibility-folds to it, like a fullwidth `（`) or any other Unicode bracket. It may not read as another local account's username, or, unless its
owner is a SysOp, as a staff title: a reserved name from §4.2 or anything
containing `sysop`. "Reads as" is §4.2's skeleton, applied after accents are
dropped, Cyrillic and Greek look-alikes become Latin letters, and everything but
letters and digits is removed, so "Ink Well", "InkWeII" and "InkWell[sysop]"
are all refused while `InkWell` is the SysOp. An alias that already exists and
reads as another name is left alone; the username beside it keeps it honest. One
holding a character reserved since it was set is not shown at all: an old
`Ann (bob)` would otherwise read as a second, forged account (issue #899). A display name follows the
same rule, but protects only SysOp names and staff titles, since it is meant to
be a person's own name and two people can share one. The rule covers aliases
this node grants. A Link or MRC name always shows its node or network, and is
not an alias.

Local chat includes bounded persistent channel scrollback, presence, away
state, invitations/membership, `/who`, `/whois`, `/names`, `/list`, `/join`,
`/leave`, `/topic`, completion, and online private conversation.

Chat's input row -- in a channel, an MRC room and a direct chat -- scrolls a
line wider than the terminal within the row, with `<` and `>` marking text out
of view, as every other prompt does since issue #546 (issue #926). The row is
the terminal's last, outside the scroll region, so a soft wrap there has no row
to go to and overwrote the line. Tab completion edits the line and the window
redraws, and the repaint after an incoming message or a status update draws the
same window around the cursor, not the head of the line.

A line from MRC is marked by an `[MRC]` badge in front of it, and its sender is
`nick@site`: the nick in its MRC color, the `@` muted, the site in a color of
its own that a linked node never uses (`MRC_SITE_COLOR`). Stored rows keep
`user@site (MRC)`; screens drop the suffix, since a parenthesis after a name
holds an account (§16, issue #899), and `/who`, `/names` and Find say
`(on MRC)`.

Chat `/help` is a width- and height-aware command table. It aligns descriptions
in one column, colors command names, parameters, and descriptions as distinct
fields, and paginates within the scrolling region while preserving chat's three
pinned rows. The bare list remains permission-aware; `/help <command>` may
explain a command outside that list without granting permission to run it.

Chat lines carry a per-user display timestamp, **on by default** and toggled
with `/timestamps`. The default is the answer for an account that has never
expressed a preference, so it applies to existing accounts as well as new
ones; an account holding an explicit `off` keeps it. Knowing when a line was
said is most of what makes scrollback readable, and a caller entering a quiet
channel cannot otherwise tell whether the last line is a minute or a week old.
The stamp is time-only, in the node's configured display timezone -- NetBBS has
one clock, and a per-user timezone is reserved for a future preferences system
rather than implemented here -- and in the metadata shade rather than the muted
one, since it is chrome attached to the line beside it and not a system message.

Because the stamp renders on every line of replayed scrollback, a carried
message's `created_at` is a field every channel entry now formats. It is
validated at the Link protocol boundary before the event is accepted, and an
already-stored value that cannot be parsed costs its own line's stamp and
nothing more: rendering must never be able to shut a caller out of a channel.

`/msg` and `/private` remain ephemeral and online-only. They never silently
fall back to asynchronous mail.

A separate mutual invite/accept direct chat also exists, alongside `/msg`/
`/private` rather than replacing either: unlike both (one-off, or a one-sided
redirect the target never agrees to), both sides must explicitly be in the
same room at the same time. Reachable from the Who screen (`[I]nvite to
chat`) or `/dm <user>` from an active channel. Exclusive with channel chat --
one active chat screen per session, the same scope Phase 2's one-channel-at-
a-time limit already establishes below. Fully ephemeral, the same as `/msg`/
`/private`: no persistence, no scrollback. An invite interrupts the main
menu live only when the recipient is idle there; otherwise it is shown the
next time they return to it. A recipient who is on any other screen is told
in a one-line notice, delivered the way a SysOp's message is, to go back to
the main menu to answer, and the inviter's waiting screen says the
invitation opens there (issue #843). A door or a file transfer that owns the
recipient's terminal gets no notice, only the waiting-screen line. The main
menu is drawn again after the invitation is handled, with a decline carried
above its prompt, because a direct chat clears the screen on its way out. For
the same reason the Who screen does not pause after a direct chat that ran.
An unanswered invite expires automatically after a short fixed window, with
an explicit accepted/declined/timed-out outcome always shown to the inviter
-- never a silent no-op.

While the inviter waits, only `C` cancels; unsupported keys are rejected and
the invite remains live. If acceptance and local cancellation become ready in
the same scheduler turn, the already-committed acceptance wins so the accepting
peer is never stranded. Entering from `/dm` fully unwinds the channel screen
before direct chat takes ownership of session input/output, then reauthorizes
and re-enters the channel afterward. A peer-leave notice is a mandatory
lifecycle signal and uses priority delivery rather than lossy chat-traffic
overflow behavior.

The direct-chat pinned status row permanently exposes the leave command (with
a compact narrow-terminal fallback). Submitting a line clears/redraws the input
row before the committed chat line is rendered, so the sender sees one message,
not the input echo plus a second room copy. Identity labels and message bodies
are sanitized and styled as separate spans using semantic theme colors.

Phase 2 uses one active channel per session. Multiple simultaneous memberships,
background delivery, and Link-wide presence wait for Phase 5.

A channel may additionally be bridged, per channel and only by explicit
SysOp choice, to one room on the external MRC (Multi Relay Chat) network
(§16, issue #165 / #275); and, where the SysOp allows it, a caller may
open any MRC room by name, which materializes a channel `mrc:<room>` of
its own — a real row with the node-wide open-room gates, listed in the
picker's own "Multi Relay Chat" section, never Link-able, and retired
once idle (§16, issue #300). Bridging changes nothing about the channel's
own model: local traffic is recorded and delivered exactly as before and
then relayed; inbound MRC lines are recorded as external, unverifiable
authors (stored as `user@site (MRC)`, shown badged `[MRC]`, §6.3) that never enter trust evaluation; private
MRC messages are never delivered. The caller is told on joining that
their handle becomes visible on that network, and a caller already inside
a channel when the SysOp maps or remaps it is told before anything they
say leaves the node. An inbound MRC line is stored with an explicit
external-source marker: it is never exported in a trusted-scrollback
snapshot under the origin's identity and never signed for Link
propagation, and a locally recorded message is always re-read by its own
row id so a concurrent bridge write cannot be mistaken for it.

Live membership is keyed by channel name, and a session inside a channel keeps
that name for everything it sends, receives, and leaves with. A channel is
therefore not renamed while callers are inside it: the SysOp screen refuses
with the occupant count until it is empty, and the standalone admin CLI, which
cannot see occupancy, states what a rename does to anyone inside.

A channel that closes while callers are inside it — the SysOp deletes it, hides
a carried Link channel (§16, issue #683), or retires an MRC room a caller
opened — moves every one of them back to the channel list at once, where the
line "#name was closed by the SysOp." is shown above its prompt; no keypress is
asked for (issue #716). Their live Link subscription and MRC presence end the
way `/leave` ends them. A closed channel gets no `leave` line: a deleted one has
nowhere to hold it, and a hidden one keeps its scrollback exactly as it was for
Restore. A close the running node cannot push (the standalone admin CLI) is
caught by the session itself the next time it sends, with the same result.

### 6.4 Personal mail

Local asynchronous mail is a persistent domain distinct from chat `/msg`.
Messages have sender/recipient views, subject, body, read state, and independent
delete state. The row is removed when neither side retains it. A side with no
local account behind it -- mail received over Link, mail from the system --
is deleted from the start, so the other side's delete removes the row.

**Deleting an account** (issue #818) deletes that account's view of each of
its letters, as if its owner had deleted them: a letter nobody else can see
goes at once, and a letter the other side still has stays theirs. A sender's
Sent copy survives its recipient's deletion (`recipient_user_id` is ON DELETE
SET NULL, like `sender_user_id`), and the Sent list names the recipient as it
was then, "bob (deleted account)", from `recipient_label`, which the deletion
writes. Link mail always names its remote address instead. Outbound Link mail
of a deleted sender keeps its row, so its delivery is still tracked. Before
#818 the recipient's deletion cascaded and took the sender's copy, and a
deleted sender (or a Link sender) left rows its recipient could delete from
view but never remove. The table's CHECK still refuses a row with neither a
local nor a remote recipient unless its recipient side is deleted.

Recipient mailboxes are bounded (`MAX_MAIL_PER_RECIPIENT`, 500, counting the
Inbox). When full:

- the oldest already-read message may be evicted to make room;
- unread mail is never silently discarded;
- a kept message (below, issues #828 and #921) is never evicted, read or not,
  and is not counted: Kept has its own limit, `MAX_KEPT_PER_RECIPIENT` (100);
- if no safe eviction exists, delivery fails explicitly.

The owner can see the cap coming (issue #818): the Inbox header counts
"N of 500", and from 450 (nine in ten) the Inbox says what happens at the
cap; a mailbox full of unread mail says new mail is turned away. Each
eviction is counted (`mail_eviction_notices`), and the owner is told once,
at their next main menu, how many old read messages were removed -- a count,
never which ones, since the notice outlives them. A warning at the main menu
before anything is removed was considered and left out: the Inbox is where
the owner can act on it.

Local mail is the domain extended by Link messages; Link mail does not create a
parallel mailbox UI.

**Telling a caller that mail arrived** (issue #823). Three places, all only
for a caller mail is open to (`caller_mail_refusal`):

- *At login.* The first main menu after login says what waits, above its
  prompt, with two counts (issue #917): the unread letters that arrived
  since the caller's last call, and every unread letter -- "3 new since
  your last call, 7 unread in all -- [E]-mail to read them.", short enough
  to stay on one row at 80 columns. Both are given even when they are
  equal, which says that nothing older waits; with nothing new it reads
  "Nothing new since your last call, 7 unread in all". "The last call" is the start of the caller's previous
  session: the newest of their `session_history` rows before the current
  one (by id, so a second session of the same account open meanwhile
  counts). A caller with no earlier row -- a first call, or one whose rows
  the bounded table has pruned (twenty per account within a node-wide
  budget) -- gets the unread count alone, "You have 7 unread messages".
  Nothing unread, no line. A letter's arrival is when this node stored it:
  `created_at` for local and system mail, and for Link mail the receipt
  time kept in `sender_deleted_at`, since its `created_at` is the sender's
  signed time (#808) and a letter a relay held for a day is still new to
  its reader. The main menu's mail notices are told together in one order:
  that count, then the cap's eviction count (#818), then the
  caller's own Link mail that bounced or expired (#806). Moderation outcomes
  come before them, and a drain warning (§13.8) before those; the count of
  pending chat channel invitations and any queued `/msg` lines follow them
  (issue #923). Ahead of all of these is the outcome of a question answered
  during login (the character-set check, a first-run choice), which answers
  the last thing the caller did. First of all is the login line itself,
  "Welcome, <name> › level N › Ctrl-L redraws" (issue #949). Like every
  notice there it is shown once and not again after Ctrl-L, and it is
  written nowhere before the menu, in either redraw mode: with
  redraw-in-place on the menu's clear wiped it unseen, and with it off it
  would be shown twice. It is built after the login questions, so the
  character-set answer decides its separator.
- *In New scan.* A `Mail:` line heads the summary above the list with the
  login notice's two counts ("Mail: 3 new since your last call, 7 unread
  in all"; on a first call `Mail: N unread`), counted from the same
  previous call, so the scan -- which
  is "activity since your last visit" -- and the login notice never
  disagree; `[E]-mail` opens the mailbox from there, and the counts are
  brought up to date on the way back. A line with a key, like the
  replies to the caller, not a row: the list is places, and a mailbox row
  would renumber every board beneath it.
- *While online.* A per-session watcher (`netbbs.net.mail_arrivals`, started
  next to the account watcher) compares the Inbox's unread letters with the
  ones it has seen. It polls every five seconds, so every way a letter
  arrives is covered -- local, Link, system, another process -- without a
  hook in each delivery path; a local send also wakes the recipient's
  watchers at once. It tells the caller the way `/msg` does: through
  `Session.pinned_notice_hook` when a screen has one (chat, the SysOp's live
  monitor), so the line appears at once; otherwise as a notice for the next
  screen drawn, never written into a door (`door_active`) or an editor. An
  idle main menu or Inbox races the watcher's arrival event against its key
  read and redraws at once, so its counts and rows are current; the Inbox
  keeps its pending key read across that redraw rather than cancelling it.
  Letters are told apart by `(id, created_at)`, not by id, because the table
  has no AUTOINCREMENT; a letter marked unread again is not new. Up to three
  arrivals are named ("New mail from bob: Lunch?"), more are counted.

News of mail is drawn in the good-news colour, `theme.GOOD_NEWS_COLOR`
(issues #917, #944): the live "New mail from ..." lines, the login notice,
New scan's `Mail:` line, and the unread counts in the main menu's header and
the mailbox's, "N unread in Kept" included. New mail is good news, not a
problem, so none of these uses the warning colour; that stays for what is
one -- a mailbox nearly at its cap, the cap's eviction count, and bounced
or expired Link mail (in the error colour). #917 first drew them in the
node's accent, but its gold is a shade off the warning amber and the change
barely showed, and the main menu's unread count sits beside the caller's
name in that same gold. Good news is the palette's success green (82)
under its own name: green already means "good" in NetBBS, so a second green
would only be a shade to tell apart. Like the other semantic colours it is
not a SysOp branding slot, so a node's accent override never moves it. It
reads on a dark background as the rest of the palette does; the palette as
a whole targets dark terminals (a light background already washes out its
soft-white body text), and a client that folds 256 colours down to 16
maps it to green, still apart from the yellow the accent and the amber fold
to. In a session without colour the lines read the same, uncoloured.

There is no preference to turn the live notice off: it is one line per
letter, and callers already choose who may write to them (blocked senders,
below). A `/msg` queued for a caller outside chat is carried above the main
menu's prompt in the same way; before #823 it was written above the menu,
where a redraw in place cleared it unseen.

**Who may use mail** (issue #816). Mail has a node-wide level, `mail_min_level`
(Settings > Limits & retention, default 0, so open to every account). It
covers reading, writing and replying, to this node and over Link, as one
level: a caller who could read but not write could not answer, and one who
could write but not read would never see the reply. Below it the main menu
offers no `[E]-mail` and its header no mail count. It gates the caller, not
the recipient: mail to an account below the level still arrives, and waits
until the SysOp raises the account's level, as a board's posts wait for a
caller who cannot read them yet.

**Accounts that take no mail** (issues #816, #818). Three accounts are sent
nothing: the guest account, a disabled account and a signup still awaiting
approval. Neither of the last two can sign in to read mail, and its sender
would never learn it went unread. Local mail to one is refused at the To
prompt and at Send, in plain words ("bob's account is disabled, so it can't
receive mail."); Link mail bounces `recipient_unavailable` (§10.3), whose
wording, "that account is not taking mail at the moment", says neither which
state nor why: whether an account is disabled is this node's business, and
both states can end. The refusing node's SysOp sees the plain reason in
Refused Link mail. Mail already in an account when it is disabled stays
there for the day it is enabled again. A retired username (#594) has no
account and bounces `unknown_recipient` like any unknown name.

The guest account (§4.6) never has mail, whatever its level and whatever the
mail level says, and nothing is delivered to it. Local mail to it is refused
at the To prompt and by `send_mail` and `send_system_mail`
(`MailRecipientRefused`), Link mail to it bounces `no_mailbox` (§10.3), and a
moderator's rejection of a guest's post sends no rejection mail -- the
main-menu notice still tells whoever signs in next. A session that signed in
through guest login stays refused for as long as it lasts, even if the SysOp
turns guest login off or moves it meanwhile: the account's check reads the
current setting, so the session's login route (`authenticated_without_credential`)
is checked too. `netbbs.mail.mail_access_refusal` is the one check for the
account, `netbbs.net.mail_flow.caller_mail_refusal` adds the session's, and
`mail_recipient_refusal` is the one for the recipient; `browse_mail` makes the
caller's check itself, so no way into mail can skip it.

**Mail from the system** (issue #819). Some mail is sent by the BBS itself,
not by a person: today, a moderation rejection (§6.1). Such a message has no
sender account (`sender_user_id` NULL) and is marked `from_system`, and:
- the mailbox shows it as from **System**, in the list and on the From line,
  and its view says it is a notice from the BBS with no one to reply to. It
  has no `[R]eply` key.
- It is told apart by the flag, never by the name stored with it. "System"
  is a name self-service signup refuses, and an account a SysOp names
  "System" is still a person: its mail has Reply and never reads as the
  BBS's notice.
- It is in nobody's Sent, so the recipient's delete removes the row.
- It is local by construction -- no remote address, no Link event -- and is
  never carried over the Link.
- It counts toward the recipient's cap like any mail, but when the cap must
  make room, a read system message is evicted before any read letter, so a
  notice never pushes out mail a person wrote. Unread mail of either kind is
  never evicted; a system message that finds the mailbox full of unread mail
  is not stored (a rejection's main-menu notice still tells its author).

The alternative was a reserved account that cannot log in. It was rejected:
it would own rows (ON DELETE, quotas, an account list entry to hide), Reply
would open a letter to it, and its name would be one more thing a
look-alike could imitate. A NULL sender plus a flag fits the table's
existing "no local sender" shape. Mail from before the change that a
moderator's account sent stays theirs.

**SysOp tools, and what they do not show (issue #820).** Operations → Mail
shows how full each inbox is -- letters, unread, read, kept (#828) and system notices per
account, the Inbox against `MAX_MAIL_PER_RECIPIENT` (Kept is outside it, #921),
fullest first -- and the Link mail
this node refused (§12.4). Both show counts, account names, senders and reasons
only: no SysOp screen shows a letter's subject or body, and a refused letter's
record does not name its recipient. Mail is private between its writer and its
reader. The home node can technically read tier-1 Link mail and local mail in
its database (§4.5); the console does not turn that into a feature, and the
SysOp Handbook says so.

Mail about Link delivery (#806's bounces) is told at the main menu and on the
sent message's Delivery line, not by a system message; a bounce letter in
the Inbox could use this sender later.

**Blocked people** (issues #817, #925, #948). An account can refuse mail,
live messages and chat channel invitations from one person. A block names a local account by id, so it survives a rename, or a
Link sender by the `user@<home-node-fingerprint>` address its mail came
from (user part compared case-insensitively), never by the node's display
name, which can change. Blocks live in `mail_blocks`; deleting the blocking
account or the blocked local account removes the row; the table keeps its
#817 name although it now holds the one block list. A caller blocks from a
received letter's view (`Bloc[k] sender`, a toggle labelled by what it will
do), from a caller picked on Who's online (`Bloc[k]`, the same toggle, for a
local caller by account and for one on a linked node by
`user@<fingerprint>`), or by name from Profile > Blocked people, which lists
the blocks and unblocks them. A block affects mail from then on; mail
already received stays.

- The sender is told. Local mail from a blocked sender is refused at the
  To prompt and by `send_mail` (`MailSenderBlocked`) with "<name> does not
  accept mail from you"; Link mail bounces `blocked_by_recipient` (§10.3).
  Accepting and silently dropping the letter was rejected: mail promises
  nothing is lost without a word (§10.5), a silent drop would be the one
  refusal the sender never hears of, and it would split local and Link mail,
  whose refusal is a signed bounce either way. Being told reveals the block
  to the blocked person; that is the price of the honest answer, and the
  blocker loses nothing by it.
- `netbbs.mail.mail_sender_refusal` is the one sender-specific check, made
  beside `mail_recipient_refusal` (which says whether the account takes mail
  at all) by `send_mail`, the To prompt and `deliver_link_message`.
- Two senders cannot be blocked. System mail (`send_system_mail`) has no
  sender and never consults blocks. A SysOp of this node (a usable level-255
  account) cannot be blocked either: the SysOp answers for the node's
  accounts and has to reach them, and a block would buy no privacy from the
  person who runs the database it is stored in. The check reads the sender's
  current level, so a blocked account that later becomes SysOp gets through
  (the Blocked people list marks the block as not applied),
  and is blocked again if it stops being one. Staff below 255 are blockable.
- One list covers mail and live messages (issue #925). Before it a block
  stopped mail only, and the one tool against someone harassing a caller
  live was the direct-message opt-out, which silences everyone.
  `netbbs.messaging_preferences.live_message_refusal` is the one check every
  live path makes before delivering: `/msg`, `/private` (at entry and again
  for each line, so a block made mid-conversation ends it), `/dm` and Who's
  online's `[I]nvite to chat` (`run_direct_chat_invite_flow`), Who's online's
  one-off `[M]essage`, and an inbound Link direct message
  (`build_direct_message_deliverer`, by the sender's `user@<fingerprint>`).
  It answers the opt-out first -- the recipient's general choice, which says
  nothing about the sender -- and then the block, with the same SysOp
  exemption as mail. `/msg` and `/private` now respect the opt-out too;
  before #925 they were the one live path that did not.
- A blocked live sender on this node is told, as a blocked letter's sender
  is: "<name> does not accept messages from you". An inbound Link direct
  message from a blocked sender is dropped without an answer, as one to an
  opted-out or offline recipient already was (§8.10.3): the
  `direct_message` frame has no reply on the wire, and adding one is a
  protocol change this does not make. The remote sender sees their usual
  "(sent to ...)"; their mail, which does have a bounce, tells them.
- A block stops a chat channel invitation too (issue #948). `/invite` to
  someone who has blocked the inviter writes no `channel_invitations` row
  and sends no live notice, and tells the inviter "<name> does not accept
  messages from you", `/msg`'s words
  (`netbbs.messaging_preferences.invitation_refusal`). The inviter's right
  to invite is answered first, so a caller who may not invite anyone hears
  that rather than learning of a block. The direct-message opt-out does not
  stop an invitation, as it never has: an invitation waits in the invitee's
  pending list and asks nothing of them, where a live message interrupts.
  The SysOp exemption is the same as mail's. `/invite` is the only path that
  creates an invitation; Link carries none. An invitation made before the
  block stays pending until it expires or is revoked.
- Who's online offers only what can succeed (issue #948, after #920's
  picker); so do the other places mail is offered from (issue #953, see
  "Mail from where callers meet"). For a local caller who has blocked the viewer it offers neither
  live action nor `[E]-mail`, since the letter would be refused, and its
  subtitle says "<name> does not accept messages or mail from you." The
  screen is still drawn, with `Bloc[k]` where the viewer may block them back
  (their block does not stop their own mail) and `[B]ack`. A caller on a
  linked node keeps `[E]-mail`: their node's block list is not known here,
  and the To prompt or the bounce answers.
- What a block does not cover. Public chat channels: a block does not hide a
  blocked person's lines in a shared room, and there is no per-caller ignore
  in chat; channel moderation (mute, kick, ban) is the tool there. An
  invitation into a channel is covered (above). MRC
  private messages come from another network's users, not accounts or Link
  addresses, and are not covered. SysOp messages (the console's message to a
  caller) and system notices never pass the check.

**The mailbox is a list; a message is read on its own screen** (issue #810),
the shape the board post list has (§6.1, issue #679). `[E]-mail` opens the
Inbox directly. Before #810 it opened a four-option menu (Inbox, Sent,
Compose, Back), so every visit cost a keystroke before any mail showed.

The list:
- Is a table: number, a `new` column, From, Subject and Date. Sent's is
  number, To, Subject, a Delivery column (pending, with relay, delivered,
  bounced, expired) when any listed message went over Link,
  and Date. Each row is numbered once. The generic picker it replaced
  numbered rows twice (`01. (#5) ...`) and prefixed unread subjects with
  `[NEW] `.
- Follows §3.6: columns are measured in display width and cut with an
  ellipsis. The name takes up to two fifths of what the fixed columns
  leave. It has no fixed cap, because a Link address carries its node's
  name, and the message view shows it in full. Below 60 columns a row
  becomes prose: "N new name: subject".
- Fits as many rows as the terminal holds and pages with `[N]ext page` and
  `[P]rev page` (PgDn/PgUp). A mailbox is bounded (§14), so the whole folder
  is loaded and paged in memory. Below 16 rows the blank rows and rules go,
  and each note above the list is cut to one row, so the 40x12 floor keeps
  three rows of mail.
- Has a cursor: Up/Down, and Enter or a digit to open a message. `[B]ack`
  from a message returns with the cursor on it.
- Has a header that says how many messages are unread and how many there
  are in all, the Inbox order, and any `[F]ind` filter. The prompt is the
  main menu's, clock and node-status tags included.
- Keeps every action the menu had on its action bar: `[S]ent` (whose
  `[B]ack` returns to the Inbox), `[C]ompose`, and `[D]raft` with the kept
  letter's notice (issue #814).
- `[O]rder` switches the Inbox between newest first and unread first
  (newest first within each), and since #828 by conversation (below). It is
  a per-caller preference (`mail_order`).
- `[F]ind` narrows the folder to mail with a word in the name or the
  subject, as the row shows them. The picker's `[S]earch` said "by name"
  but matched the subject, `[NEW] ` prefix included, so "new" matched every
  unread message. The main menu's Find searches bodies too, across both
  folders (issue #824, §6.6).
- `[U]nread` on the list marks the highlighted message unread, or read if
  it is unread; the reader's `[U]nread` marks the open message unread and
  returns to the list. Opening a message is still what marks it read.
  Marked-unread mail is kept by the mailbox cap, like any unread mail.
- Flags a sender whose node's identity changed (`_link_mail_identity_warning`)
  with `!` before the name, and says what it means above the list. The
  message view carries the full caution.

**Managing a mailbox** (issues #828, #921). The list manages letters in bulk,
keeps letters out of the cap, and lists by conversation:
- `[M]ark` (or Space) toggles a mark on the highlighted letter and moves the
  cursor down, so a run is marked key by key. The mark is a `*` in the column
  after the cursor's (before the number in prose rows), and the header counts
  the marks. Marks belong to the folder on screen and go when it changes.
- `De[l]ete` deletes the marked letters, or with none marked the highlighted
  one, behind one yes/no that names the count (or the subject). `Delete [r]ead`
  in the Inbox deletes every read letter there, behind one yes/no with the
  count; unread and kept mail stays. Both delete the caller's own side only,
  with the one-letter rule (`netbbs.mail.delete_letters`): a letter whose
  other side is gone is removed with its `mail_search` entry (§6.6), one the
  other side still has is marked deleted.
- **Kept** is a third folder beside the Inbox and Sent (`[K]ept` from the
  Inbox, `[B]ack` returns). A folder rather than a filter: kept mail is mail
  the caller has dealt with and wants out of the way but safe, and a filter
  would leave it in the Inbox. `K[e]ep` in the Inbox, on the list (marked or
  highlighted) or in a letter's view, moves letters there;
  `Mov[e] to Inbox` moves them back. Nothing is lost either way, so nothing
  is asked. `mail_messages.kept_at` records it; it is the recipient's alone,
  since Sent is never evicted.
- A kept letter is never evicted by the cap (`make_room` skips it) and does
  not count toward `MAX_MAIL_PER_RECIPIENT` (issue #921). Kept has its own
  limit instead, `MAX_KEPT_PER_RECIPIENT` (100), so each caller holds at most
  600 letters, and a mailbox cannot grow without bound (§14) one kept letter
  at a time. #828 first counted Kept toward the 500; that made the cap about
  "unread and kept mail", a limit the caller could not see coming from the
  Inbox and one that kept letters quietly ate into, so the operator chose a
  separate, visible limit. The cap is about unread mail again: the Inbox
  header's "N of 500" counts the Inbox alone (and says how many are in Kept),
  a mailbox full of unread mail refuses new mail (`mailbox_full`), and moving
  a letter to Kept makes room in the Inbox. The Kept folder's header counts
  "N of 100", and a full Kept says so above its list.
- Keeping when Kept is full is refused in place, with nothing moved: "Kept is
  full (100 letters) -- move some back to the Inbox or delete them first."
  (`netbbs.mail.KeptFullError`, raised by `set_kept`). A bulk keep of marked
  letters is all or none: when they do not all fit, none move, the marks stay,
  and the caller is told how many more Kept has room for. Keeping as many as
  fit was rejected: it would split the marked letters between the folders by
  list order, leaving the caller to find which went where -- the same reason a
  letter to several people is all or none (#827). Moving letters back to the
  Inbox is never refused. It can take the Inbox past 500 by what was kept;
  nothing is removed for that, and new mail then arrives only in place of a
  read letter, so the account still holds no more than the two limits
  together. Kept counts are local: no Link payload carries them. The SysOp's
  Mailboxes screen has a Kept column (flagged at 100), and its "Of the cap"
  and fullest-first order count the Inbox alone.
- `[O]rder` cycles newest first, unread first and by conversation (Sent:
  newest first and by conversation), one per-caller preference. Replies record
  no parent letter, so a conversation is the correspondent (the sender in the
  Inbox and Kept, the recipient in Sent; the system is its own) plus the
  subject with every leading `Re:`, `Fwd:` or `Fw:` removed -- the prefixes
  `netbbs.quoting` writes -- compared case-insensitively
  (`netbbs.mail.thread_key`). Conversations are listed by their newest letter,
  and each one's later rows are indented under it. A reply-to id would group
  a renamed subject too; it was left out as not worth a schema change and a
  Link payload field while subjects carry the conversation.
- At the 40x12 floor the action bar takes four rows, so below 16 rows the
  notes above the list share one row, the most urgent (the cap's, then a kept
  letter's, then the identity note); what the others say is on screen in brief
  (the `[D]raft` key, "N of 500" or "N of 100", a row's `!`). Below 60 columns the header
  uses short counts and leaves out the order and the Kept count.

A received message's view names its recipient: `From:`, `To:` (the reader)
and `Date:`, as a sent message's view has `To:`.

**One letter to several people** (issue #827). The To field takes up to
`MAX_MAIL_RECIPIENTS` (20) addresses, local and Link mixed, separated by
commas; a comma inside double quotes (a quoted node name) does not separate.
Each address gets the To prompt's checks as it is typed, and a refused one is
named, with the typed list given back to fix. Tab completes the address after
the last comma, and `?` as the last address opens the list for it.
- **One copy per recipient.** Send writes one ordinary letter per recipient:
  a local row by `send_mail`'s rules, a Link row by `compose_link_message`'s.
  Each copy has its own delivery state, bounce, cap handling, block and
  refusal, and every rule of this section applies to it unchanged. The
  alternative -- one letter row with a recipient table -- was rejected: every
  per-side rule (deletion, Kept, read state, the cap, delivery, search) is
  per row today and would have had to learn about recipients.
- **The copies are linked** by `mail_messages.mail_group_id`, and each
  carries the whole To in `mail_group_to` (JSON: an account by id and name,
  shown by its current name; a Link address; or `{"all": true}`). The list
  is stored with every copy rather than read back from the others, because a
  copy both sides deleted is gone and the To must not lose a name with it.
  There is no blind copy: everyone sees everyone.
- **All or none at Send.** Every copy is checked -- the account takes mail,
  has not blocked the sender, has room; the Link node has keys on file --
  before any is written, and all are written in one transaction
  (`netbbs.mail_groups.send_letter`). A recipient who cannot take it is named
  on the review screen and nothing is sent; `[T]o` drops or fixes them.
  Sending to the others anyway was rejected: the copies already written would
  list someone who never got the letter, and a second Send would reach the
  others twice. Link delivery is later and per copy, so a Link copy can still
  bounce on its own.
- **Never twice.** The group id is chosen when the letter is started and
  kept with its draft; a group id already sent is refused
  (`letter_already_sent`), so a draft sent again after a dropped connection
  reaches nobody twice.
- **Over Link** the To goes inside the sealed plaintext beside the subject
  and body: `"to"`, everyone as `user@<home-node-fingerprint>` (this node's
  own accounts by its own fingerprint), and `"group"`, the group id. It is
  not a signed payload field, so no capability is needed: a node that does
  not know the keys reads the subject and body and shows a letter to one
  person. The receiving node keeps the list only if it reads cleanly (at
  most `MAX_MAIL_RECIPIENTS` well-formed addresses and a short alphanumeric
  id); anything else is dropped, not bounced, since the list is only
  something to show. Its own accounts become local entries, and the group
  id is stored as `<id>@<sender fingerprint>`, so no other node's letter
  can share it.
- **Reading.** Each copy's `To:` is the whole list. `Reply [a]ll` on a
  received copy writes to its sender and everyone else on it but the reader;
  `[R]eply` stays the sender alone. Sent lists the letter once, by its newest
  copy, with the list under To and the status of the copy that needs the
  caller most (bounced, expired, resent, pending, with relay, delivered;
  issue #919). Its view
  gives each Link copy a `Delivery to <name>:` line; `[R]eply` writes to them
  all again, `Re[s]end` only to the copies that bounced or expired, and
  `[D]elete` (on the view or the list) removes every copy from Sent. Find
  shows it once in Sent, matched by anyone on it. A follow-up the caller did
  not type (Reply all, Reply, Resend) checks each address first and leaves
  out, by name, anyone who can no longer be written to.
- **Mail to all callers.** A SysOp writes to every account that takes mail
  from Operations → Mail in the console: `[W]rite to all callers` from their
  own account, signed, repliable and in their Sent as one letter, or
  `[N]otice to all callers` from the system (issue #819), unsigned and not
  repliable. The recipients are the address book's: every account
  `mail_recipient_refusal` and `mail_sender_refusal` let through, but the
  sender's own (the system's notice reaches the SysOp too). Unlike a letter
  to several people, a full mailbox does not stop it: that recipient is
  skipped and named in the outcome, with the count of accounts left out.
  All copies go in one transaction, and the group id makes it never-twice
  like any group. Each copy's To reads `Everyone on this BBS`, and it has no
  `Reply all`. It is local only. It lives in the console rather than the
  mailbox because it is the SysOp acting for the BBS, as the console's other
  mail tools are; a To-prompt keyword for everyone would be one no caller
  could find and a SysOp could type by accident.

**Read receipts** (issue #829). The sender of local mail sees in Sent when
each recipient first opened it.
- **On by default, opt-out, reciprocal.** Profile's `E[x]change read
  receipts` ("Let senders see when I've read their mail", the user
  preference `mail_read_receipts`, default on) is one switch for both
  directions: off, no sender sees when the caller read their mail, and the
  caller sees no one's receipts. Opt-in was rejected as a feature nobody
  would turn on; one-way opt-out (hide mine, still see theirs) was rejected
  as unfair, the model most messengers use too.
- **The opted-out marker.** A recipient who does not share receipts is
  always named as such in the sender's view (`Read: not shown, as bob
  doesn't share read receipts`, or on a letter to several people `Don't
  share read receipts: bob`) -- also to a sender who has opted out
  themselves -- and Sent's list says `no receipt`. It reveals only a
  setting, and without it a letter never reported as read would be taken for
  one not read yet. A sender who does not share receipts is told everything
  else only as "not shown" in the view, and their list has no read states at
  all (the list's column would otherwise say nothing but `no receipt`).
- **What a receipt says**: the time of the *first* reading
  (`mail_messages.first_read_at`, set by `mark_read`, kept through Mark
  unread and later readings -- a receipt once given is not taken back by
  hiding the letter from oneself), or `not yet`. A letter the recipient
  deleted unopened is `not yet` too (issue #922): their deletion is theirs,
  and "deleted unread" told the sender more than whether it was read. A copy
  whose recipient's account was deleted shows none.
- **Shared then and shared now** (issue #922). The first reading records
  whether the sender and the recipient both shared receipts at that moment
  (`mail_messages.first_read_shared`). `netbbs.mail.read_receipts` shows a
  receipt only if both shared then *and* both share now. So turning
  receipts off hides those already given and turning them on again shows
  them again, but a reading made while either side had them off never
  becomes a receipt: a sender who keeps receipts off cannot switch them on
  for a moment to see what was read in the meantime, and a recipient who
  reads while opted out does not hand that reading over by opting back in.
  Such a reading shows as `not yet`. Deciding purely at display time (the
  #829 model) was rejected for exactly that peek. Letters read before the
  upgrade keep their reading as their receipt: the migrations copy
  `read_at` and count it as shared, as receipts were on for everyone by
  default, and a caller who minds turns them off. (A reading whose sender or
  recipient has receipts off at the upgrade is not counted as shared: only
  a database that ran the unreleased #829 code can hold one, and it keeps
  no record of when the switch was made, so the backfill errs toward not
  showing.)
- **Local mail only.** Link mail carries no receipt (nothing goes over
  Link), and neither does mail from the system, which has no sender to tell.
- **Sent.** A letter to one person has a `Read:` line under its To. A
  letter to several people groups its local copies by receipt -- `Read by:`
  (each with its time), `Not read yet:`, `Don't share read receipts:` --
  which keeps its twenty recipients to three lines beside
  the Link copies' Delivery lines. Mail to all callers only counts: `Read:
  by N of the M who share read receipts (K more don't share them)`. The
  list's Delivery column, headed Status once it shows a receipt, says
  `read`, `not read`, `some read` (a group, of the copies that report) or
  `no receipt`; a Link copy that bounced, expired, was resent (#919) or is
  on its way still wins, as the more urgent state.

**Files in a letter** (issue #830). A letter can point at files already in
this node's file areas; there are no attachments of new files and no new
storage. The model is `netbbs.file_refs`, meant to be reused as it is by a
board post that points at a file (issue #842 F086); the screens are
`netbbs.net.file_ref_view`.
- **What is stored.** One `mail_file_refs` row per file per letter row:
  the file's content-addressed `file_id` (which no later upload reuses;
  `files.id` can be), and its name, area name and size as they were when
  attached. A file deleted since is still named. At most `MAX_FILE_REFS`
  (5) per letter. The body is left as the writer wrote it.
- **Attaching.** `[A]ttach file` on the review screen picks a file area the
  writer may read, then a file in it (approved, not past its age);
  `[R]emove file` takes one off. The files are kept with the draft. A
  forward carries the files of the letter it passes on, those the forwarder
  can open; a reply, Reply all and Resend carry none.
- **Who may be sent it.** Every local recipient must be able to read the
  file area of every file: its read level or grant, and its age requirement
  (`may_read_area`, the file areas' own gate). Checked for each copy by
  `mail_groups.recipient_problem` before anything is written, so a letter to
  several people refuses at Send, naming who cannot open which area, and
  sends nothing -- the same all-or-none rule as a full mailbox. Sending to the
  others with the file left out for one was rejected: every copy has the same
  body, and a reader would read about a file they were never given. Mail to
  all callers instead skips and names callers who cannot open a file, as it
  does full mailboxes: a letter to everyone must not be held back by a few.
  The writer must be able to open each file at Send too.
- **Who may download it.** The letter's view lists the files under Date and
  `[G]et file` downloads one through the file areas' own download
  (`file_flow.send_file_to_caller`: Zmodem, else a browser link, and a
  browser link when a Zmodem send fails), after `open_ref` checks again. A
  reader who may no longer read the area -- its level raised after sending --
  sees "A file in a file area you can't open", never the file's or area's
  name. A file deleted, expired (by status or already past its area's age),
  in an area removed or hidden (issue #683), or with its content gone shows
  as "no longer available".
- **Local only.** Nothing about a file crosses Link but text. A Link copy has
  no reference rows; its body ends, after a blank line, with one line per
  file: `File: <filename> (<size>) in file area "<area name>" on <node
  display name>` (`link_text_line`), size as the file areas show it (B, KiB,
  MiB, GiB). Those are the facts a reader on another node can use to find the
  file by hand; the `file_id` is left out as meaningless to them. Received
  Link mail never has references.
- **Removal.** `mail_file_refs` has no foreign keys. One to `mail_messages`
  would make that table a foreign-key parent, and a rebuild of it (as
  migration 97 was) would then cascade through the references on DROP
  TABLE's implicit DELETE. Rows go with their letter in
  `netbbs.mail._remove_letters_without_commit`, the one place a letter is
  deleted for good, beside its `mail_search` entry.

**How a body reads** (issue #809). A letter keeps its writer's lines: the
message view, Sent's view and the review screen show every line as written,
and wrap only a line wider than the terminal, at a word
(`netbbs.rendering.post_body.lined_body_rows`). Mail used to reflow a body the
way a board post then reflowed. That ran a greeting into the first sentence, a list
into one line and a signature into `-- Alice of Q Pen club treasurer`. A
letter's short lines are its form, not text to rewrap. `>` quote lines are
muted and keep their marker when they wrap. Board posts read the same way
since issue #837.

A body is filtered exactly as a post on a board that allows color (§6.1,
"Color in posts"): pipe codes and SGR color show, and every other escape
sequence is removed whole. Local mail and mail carried from another node take
the same path, so a peer can no more clear a reader's screen through mail than
through a post. There is no SysOp switch for mail: a letter is between its
writer and its reader, and the filter is what makes color safe to show. The
reader's own "Post colors" preference covers mail too. With it off, a letter
shows as plain text, the codes removed. Both mail editors keep pasted color
as pipe codes, as a board that allows color does. A reply quotes the plain
text, without codes, as a board reply does. Stripping codes on display was the
alternative. It was rejected because boards already show them safely, and a
writer who typed `|12` meant color.

**Mail from where callers meet** (issue #821). A caller writes to someone from
the screen where they found them, without typing an address:
- the Directory's member card, `[M]ail`;
- Who's online, `[E]-mail` on a selected caller (`[M]` there is the live
  message), for a local caller and for one on a linked node;
- Previous callers, `[M]ail a caller`, which asks for the row's number;
- the board reader, `[M]ail author`: a private reply to the post's author, with
  the post's `Re:` subject and quote, as a board reply has (§6.1). It is offered
  to anyone who may read the post, whether or not they may post there, and has
  a draft slot of its own per post, apart from mail replies.

All four open the compose screen (§3.5, issue #813) with To filled in, through
one entry point (`netbbs.net.mail_flow.mail_someone`) that makes the mailbox's
own checks: `caller_mail_refusal` for the caller, `mail_recipient_refusal` for
a local recipient, and for someone on another node the check a Link reply's
address gets (issue #805) -- the address is their stable
`user@<home-node-fingerprint>` (Who's online's presence, a carried post's
author label), shown by the node's current name. The action is not offered
while mail is closed to the caller, on the caller's own card, post or call,
for a deleted account, or for a carried post's author while Link is off; a
recipient-side refusal (the guest account, a peer on probation, a node this one
is not linked with) is said when the key is pressed. Opting out of direct
messages (§6.3) does not close mail: Who's online still offers `[E]-mail` for
such a caller. A Previous callers row whose name the roll hides is not
mailable, since To would show the name; a SysOp, who sees every name there,
may write to any. After the letter is sent, kept or given up, the caller is
back on the screen they came from with the outcome above its prompt.

Nor is mail offered to a local account that has blocked the caller (§6.4
Blocked people; issues #948, #953), since the letter would be refused.
`netbbs.net.mail_flow.mail_blocked_notice` decides it for every screen, from
`mail_sender_refusal`, so a SysOp of this node is never refused. Where the
screen has room it says "<name> does not accept messages or mail from you.":
Who's online as its subtitle, the member card above its action bar. The board
reader has no line for it and simply leaves `[M]ail author` off. Previous
callers keeps `[M]ail a caller`, which belongs to the whole roll, and refuses
that caller's row with the sentence when its number is typed. Hiding the key
only when no row is mailable was rejected: every other row would have to be
checked for the rare roll of callers who all blocked the viewer, and the
refusal already comes before any compose screen. Someone on a linked node is
offered mail as before: their node's block list is not visible here, and the
To prompt or a bounce answers.

A letter started from the Directory, Who's online or Previous callers is the
caller's new letter, the same slot `[C]ompose` uses: a kept new letter is
offered first, and resuming it keeps its own recipient.

**Finding a recipient** (issue #826, `netbbs.net.mail_recipients`). The To
prompt helps without leaving it. Tab completes the field (the line editor's
completion, as chat's): a member's name, the address of a recent
correspondent, and after `@` the name of a linked BBS; several matches are
listed under the prompt, wrapped, and more than 24 are counted instead. `?`
and Enter opens a list (`pick_item`) of everyone the caller can write to:
recent correspondents first, then the members, then the linked BBSes. On a
node with Link on, `name@?` lists just the linked BBSes for that name, and a
BBS chosen from the full list asks for the user name there. `?` works at the
review screen's `[T]o` too.
- Recent correspondents are the last ten people named by letters still in the
  caller's own Inbox and Sent (`netbbs.mail.recent_correspondents`), local and
  Link. System mail, a deleted sender and letters to oneself name no one.
- The list also shows who the caller can't write to right now, with the
  reason, and does not let them be picked (issue #920): someone who blocked
  the caller, "doesn't accept your mail" (`mail_sender_refusal`); a disabled
  account, "account disabled", and a signup awaiting approval, "awaiting
  approval" (`mail_recipient_refusal`); and on Link a met node still on
  probation (§12.4), "not linked yet". Such a row is muted, has `-` where
  the number goes and takes no number, so the numbers count only what can be
  picked; the highlight steps over it, and a search whose one match it is
  shows it rather than choosing it (`pick_item`'s `selectable_of`). Each row
  is one line either way, so the page budget is unchanged at 80x24 and
  40x12. Still left out: the caller, the guest account, which has no mailbox
  rather than a state that can change, and a node whose mail the SysOp closed
  (quarantined or blocked). Leaving people out made a missing name look like
  a typo or a missing account; the operator accepted that the list tells
  callers about blocks and account states, which the To prompt's refusal
  already said to anyone who typed the name.
- The reason is the same check's: `MailRefusal` carries the To prompt's
  sentence and the list's words together (`mail_recipient_refusal_detail`,
  `mail_sender_refusal_detail`, `link_mail_refusal`), so the two can't drift
  apart.
- Tab offers only what can be picked: a completion is an address the To
  prompt would take. The list is where a caller learns why someone is
  missing from it.
- A pick is only text in To: the To prompt checks it exactly as a typed
  address, and Send checks again. Nothing new is checked, so `mail_someone`
  keeps the same checks as the To prompt.
- The book is gathered once as the prompt opens: completion runs inside the
  line editor, which cannot reach the database lane.
- A Link address is kept by its node's technical identity once the To prompt
  has resolved it (a pick from the list is one already), so Send and a
  resumed draft reach the node the caller chose even if another node takes
  its name meanwhile. `[T]o` opens on the name the caller reads, and
  leaving it unchanged keeps that identity. Tab types the shortest name that
  names only that node: its friendly name, else its DNS name, else its
  fingerprint.

**Forwarding** (issue #822). `[F]orward` on a received letter's view and on a
sent letter's view starts a new letter titled "Forward": Subject gets `Fwd: `
unless it already starts with `Fwd:` or `Fw:` (the `Re:` rule, one helper in
`netbbs.quoting`), and the body is the letter under a header --
`---------- Forwarded message ----------`, then `From:`, `To:`, `Date:` and
`Subject:` as the view names them (a Link address by its node's current name,
system mail as **System**, the date in the forwarder's format), a blank line,
and the body. The caller types the recipient at the To prompt, which makes
every check a new letter's does, so any letter goes to a local account or a
Link address alike. Both editors start above the letter, where a note goes
(the line editor as after `/insert 1`; `/end` leaves it), and the forwarder's
signature closes that note, above the rule (`netbbs.quoting.sign_forward`):
appended at the end, it would read as the forwarded letter's writer's. A
reply to a forward quotes it without that signature, so the quote still stops
only at the forwarded letter's own.
- The body is carried verbatim, not quoted. A forward passes a letter on for
  someone else to read: `>` would mark it as text being answered, and the
  quote's bounds (40 lines, 8,000 bytes, stop at the signature) would cut what
  the forward exists to carry. Escape sequences are removed; color pipe codes
  stay, so it reads as the original did.
- Nothing extra bounds it: a letter at the body limit is over it once the
  header is added, and the review screen says so in characters and refuses
  Send until `[B]ody` shortens it (issue #812), as for any over-limit letter.
- System mail can be forwarded. Passing a moderation notice on -- to the SysOp,
  say -- harms no one, and the header says it came from System.
- `caller_mail_refusal` is checked when the key is pressed. Each letter's
  forward has its own draft slot, apart from a reply to it.
- Where it returns (issue #919): a forward sent from a Sent letter closes
  the view as Reply and Resend there do (below) -- the Sent list, on the
  letter, or Find's results, with "Message sent." above the prompt. The
  new letter is in Sent, and a view left open would stand on the old one
  as though nothing had happened. A forward from the Inbox comes back to
  the letter's view, as a reply to it does.

**Reply and Resend on Sent** (issue #825). A sent letter's view is
`[R]eply Re[s]end [F]orward [D]elete [B]ack`; both new keys write to the
letter's recipient through `mail_someone`, so the checks a letter started from
a meeting place gets are made when the key is pressed, and Send makes them
again (`send_mail`'s recipient and block checks, `_check_link_reply_address`
for a Link address). A recipient whose account was deleted since (#818) is
refused at the key, by the name Sent shows. After a letter is sent the view
closes and the screen it was opened from -- the Sent list, the new letter on
it, or the main menu's Find results (#824) -- shows "Message sent." above its
prompt; a cancelled, kept or refused one comes back to the view.
- `[R]eply` is a follow-up: `Re:` by the reply rule, and the caller's own
  letter quoted under "<caller> wrote:" as a reply to a received letter is
  quoted. Quoting one's own letter was chosen over an empty body: the
  recipient may have deleted it, and the quote is one keystroke to remove. It
  shares the reply draft slot scheme (`_reply_key`), so it is offered only for
  that letter.
- `Re[s]end` is offered only on Link mail that bounced or expired (any
  reason, `no_answer` included). Mail that was delivered, is pending or is
  with a relay may reach its reader, and local mail cannot fail after Send, so
  a second copy there would only be a duplicate; the caller can still forward
  it or write anew. The new letter is titled "Resend", goes to the stored
  `recipient_remote_address`, and carries the subject and body verbatim --
  not quoted, not `Fwd:` -- with escape sequences removed and color pipe codes
  kept, as a forward's body is. The signature it was sent with is part of the
  body, so none is appended again. It has a draft slot of its own per letter
  (`mail_resend_<user>_<key>.draft`), apart from a reply to or a forward of
  it. The failed row keeps its status and reason, and opening it still
  clears its notice flag (#806).
- A resent letter says so (issue #919). Once the new letter is sent, the
  failed row gets `mail_messages.resent_at` (`netbbs.link.mail.
  record_resend`); a second resend moves it to the later time. Sent's
  Delivery column then reads `resent`, the view adds `Resent: <date> (the
  new copy is in Sent)` under its Delivery line, and the key reads
  `Re[s]end again`. Only a bounced or expired Link row of the sender's is
  marked, and only when the new letter went to that row's recipient: the
  review screen's `[T]o` can send the resend to someone else, and then the
  old letter did not go again. The marker is a display over the stored
  state, not a new `link_delivery_status`, so every delivery path, expiry
  and notice keeps treating the row as the failure it is; a late answer
  that makes it `delivered` (a relay's `no_answer` expiry, #874) shows as
  delivered, with its `Resent:` line kept. Recorded after the send, in a
  write of its own: a crash between the two leaves a sent resend unmarked,
  which a second Resend would repeat, and nothing worse.
- A letter to several people records the resend on each copy the new
  letter reached, by the same address rule. `Re[s]end` goes to the failed
  copies not yet resent; once every failed copy was, the key reads
  `Re[s]end again` and goes to them all. The view gives each resent copy a
  `Resent to <name>: <date>` line under its own Delivery line.
- `resent` ranks as a failed letter. In a group's one row the order is
  bounced, expired, resent, pending, with relay, then a read receipt
  (#829), then delivered: a failure still waiting on the caller comes
  first, so the row reads `resent` only when every failed copy was resent,
  and a failure the caller already dealt with still outranks a copy on
  its way and a local copy's receipt. It is shown muted, as pending is.
  Receipts never meet it on one copy: only local copies have receipts,
  and only Link copies can be resent.

### 6.5 Communities

A Community is a topic-oriented coordination/container object above boards,
channels, and file areas. It does not merge those domains or change their
behavior.

Each board, channel, or file area has zero or one Community. “Uncategorized” is
the absence of a Community, not a synthetic row. Categories remain a separate
layer below Communities.

The SysOp orders Communities (issue #838): the callers' Communities list
follows each one's `position`, not its name, and a new Community goes last. A
Community's console screen moves it up or down and shows its place. Nodes
upgraded from before this keep the alphabetical order they showed. The first
field test's SysOp named a Community "The Clubhouse -- Start here" and watched
it sort last, with nothing she could do about it.

Boards and file areas have a SysOp order too (issue #839), and it is what a
caller's list shows unless the caller picks another under `[O]rder`. The old
default re-sorted by latest activity on every visit, so the field test's
caller found another board at the "03" he remembered a minute later. A new
board or area goes last, including one carried over the Link. The console's
`[U]p`/`[D]own` move it among the boards or areas that share its category,
Community and pinned flag -- a swap with any of those shows in every caller's
list that holds both, where a swap with a board of another Community would
change nothing in that Community's list -- and its screen shows its place; `[R]emove` deletes it, as on a
Community's or a category's screen. The console's own lists follow the same
order. Activity, name, newest and volume stay available as a caller's
`[O]rder` choice. Channels keep their alphabetical default: they have no
stored order to follow.

Categories (for boards, file areas and chat channels, each kind independent)
are at most two levels deep. The SysOp orders them: every listing follows a
category's `position` among its siblings, not its name (issue #681). The
console lists each sub-category under its parent. Picking a category lets the
SysOp rename it, change its description, move it under another top-level
category or to the top level, move it up or down, or remove it. The two-level
rule holds on every edit: a new parent must itself be top-level, and a category
with sub-categories stays top-level.

Communities provide:

- topic-first navigation;
- description and visibility;
- inherited level, age, and name-verification defaults;
- Community-scoped blanket moderator grants;
- a future unit for Link carry and governance.

The main menu (issue #838) offers content two ways:

- **By kind:** `[M]essage boards`, `[C]hat`, `[F]iles` and `[G]ames` open
  the whole node's list of that kind, whichever Community each item belongs
  to. They come first, because they are what callers who know other BBSes
  look for. M, C and F are always shown; Games only while a door is visible.
- **By topic:** `C[o]mmunities`, shown while at least one Community is
  visible, lists them; picking one opens its page -- its description and an
  entry per kind it holds, with how many -- which leads to the same board,
  channel, area or door browsers scoped to that Community. Back from a
  Community's page returns to the Communities list.

"Uncategorized" (no Community) is a data-model term only. A resource with no
Community is listed under its kind like any other, so there is no menu entry
for "resources outside a Community". The earlier design had one, next to a
`[J]ump to...` type picker; a first-time SysOp created a Community only to
escape the word, and callers read Jump as a name search (field test, #831).
Both were removed. `[/] Find` holds the slash because `[F]` is Files, and `[?]`
is kept for the main menu's help entry (#840).

A SysOp on a node with no boards, channels or file areas at all sees, and
nobody else does, where to create the first one. Resources unrelated to
Communities—mail, directory, profiles, preferences, and administration—retain
their own navigation.

Community-scoped category views must filter at the query layer so a category
used by resources in several Communities does not leak another Community’s
resources into the current view.

Deleting a Community:

- sets member resources to no Community;
- revokes Community-scoped blanket grants;
- shows the blast radius before confirmation.

Existing nodes migrate safely because the nullable Community reference leaves
all existing resources without a Community until a SysOp assigns them.

#### Link Communities

A Link Community is the same Community object announced through a signed Link
event, not a separate table or local type.

Two same-named Link Communities from different origins remain distinct.
Existing local Communities may be promoted into Link scope.

Carrying a Link Community is intended to carry its present and future member
resources by default, while retaining visible per-resource and whole-Community
local exclusions. Origin defaults are recommendations; carrying-node overrides
win locally.

Actual Link Community event schemas, signed membership changes, and advanced
governance are Phase 6 work.

### 6.6 Activity, unread state, follows, and search (issue #56)

A topic-first Community hierarchy is only more useful than a plain directory if
a user can tell what changed since their last visit. This section is the
complete answer to issue #56: read/unread semantics, follow state, a
new-activity surface, and local search. It replaces §6.1's earlier vague
"local search/navigation foundations" phrase.

#### Read/unread state

Local mail already has a complete, working model: a per-message `read_at`
timestamp, a live `unread_count` query, and independent sender/recipient
deletion. A delivered Link message is a normal row in the same table, so it
already has full read tracking the moment it lands in a mailbox. Nothing new
is needed for mail; issue #56's mail bullet is already satisfied.

Boards, file areas, and channels need a per-user, per-container **read
cursor**, not an unbounded per-item flag — per-item read state for a
potentially unbounded board would itself be an unbounded table. One table,
`user_read_cursors`, holds the cursors, with an
`(user_id, object_type, object_id)` primary key, where `object_type` is
`board`/`channel`/`file_area` and `object_id` is that resource's own local
integer id (the same id `community_id`/category columns already reference —
never the content-addressed `post_id`/`file_id`, which only identifies one
item, not a container). Its payload is the newest item's ordering key the
user has already seen:

- **boards** work differently since issue #710: a post is read once opened,
  so a board cursor's arrival id is a *floor* and a second, bounded table
  (`user_board_opened_posts`) holds the posts opened above it. The model is
  "Boards: a post is read once it is opened" below; the rest of this list
  describes file areas and channels;
- file areas already page with a stable `(created_at, file_id)` keyset
  cursor (the existing file-listing implementation) — the read cursor
  stores exactly that same tuple shape, so "what's unread" is the identical
  tuple comparison keyset pagination already performs for `after=`, just
  anchored at the user's own cursor instead of a page boundary;
- channel scrollback has no revision concept and is already ordered by a
  plain monotonic message id, so a channel's cursor is just that id.

An **edit never resets read state**: an edit's root post keeps the original
`created_at`/`post_id` and row id (§6.1), which is exactly what the read
state keys on — a post a user has already read stays "read" after a later
typo fix, matching normal reader expectance. **Expiry and deletion cannot
corrupt a cursor**: an expired post keeps its `post_id` reachable until
nothing references it, and even final hard-deletion only ever removes an
already-fully-dereferenced row — a stored cursor value is a stable position
marker being compared against, never a live foreign key, so it cannot dangle
or resurrect deleted content.

A resource with no cursor row for a user has never been visited by them.
First visit — not a retroactive backfill — establishes the baseline: viewing
a file-area page or a channel's current scrollback advances that user's
cursor to the newest item they were just shown, and entering a board counts
every post already on it as read (issue #710). This is also the complete
migration story for existing accounts (issue #56's last acceptance
criterion): the read-cursor table starts empty for everyone, including
existing users, at upgrade time. Nobody's history is scanned or backfilled;
the first real visit after upgrade sets the baseline, so only genuinely new
activity from that point forward counts as unread — never a flood of
years-old "unread" content on the first login after this ships.

A never-visited resource is surfaced as **not yet visited**, not as a
specific (and potentially enormous, meaningless) unread count — a real
numeric unread count only exists once a baseline cursor is established.

Channel scrollback is a bounded ring buffer (§6.3): a channel's cursor can
only ever express "unread among what's still retained." A message trimmed
out of scrollback before a user's next visit is simply gone, the same as it
already is for a session that was never connected to see it live — this is
an existing, accepted limitation of chat's ephemeral model, not a new gap
introduced here.

**Replies and mentions** need no new schema. A board post's existing
`parent_post_id` already names the post it replies to; "replies to me,
unread" is the same cursor-filtered query further restricted to posts whose
`parent_post_id` belongs to one of the user's own posts, run across every
board the user can read rather than one at a time. A channel "mention" is a
lightweight, unverified `@username` substring match against
`channel_messages.body` for messages newer than the user's channel cursor —
a convenience heuristic, not a structured or security-relevant feature; a
literal `@alice` typed with no intended addressee is an accepted false
positive, and a message directed at someone without using their exact
username is an accepted false negative.

**Node-local arrival order for carried content (issue #72).** The
model above compares a post/file's own `created_at` against the
cursor. That is correct for locally originated content, created in the
same order it becomes visible, but not for a Link-carried post: a
remote author's claimed `created_at` can be arbitrarily old if the post
only reaches this node after a partition or a delayed catch-up, and
comparing against it can let a genuinely new arrival silently sort
behind an already-advanced cursor. `posts`/`files` rows already carry a
second, distinct ordering with no schema addition needed: SQLite's own
`INTEGER PRIMARY KEY` rowid, assigned in strict insertion order
regardless of whether a row was created locally or materialized from a
carried Link event (the same property GitHub issue #68 already relies
on for edit-chain tie-breaking). `user_read_cursors` gains
`last_seen_arrival_id`, populated from that rowid; `unread_post_count`/
`unread_file_count`/`unread_replies_to` compare against it instead of
`created_at`, while `file_area_read_cursor` (feed-position jump-to)
still compares `created_at` -- the two concerns use different orderings
on purpose, per this section's own distinction between authored
chronology and node-local availability. (`board_read_cursor` was the
same until issue #710, which computes a board's jump from its unread
posts instead; see "Boards: a post is read once it is opened" below.)
Unread counting for boards has also moved on since: #710 makes the
arrival id a floor with an opened set above it.
Existing cursors are backfilled from the post/file their existing
`last_seen_stable_id` already names, so an upgrade preserves exactly
what a user had already read rather than resetting anyone to
all-unread.

**Accepted scope boundary (file areas):** jump-to-first-unread can
still land on a file area's ordinary newest page rather than navigating
precisely to an out-of-order arrival buried elsewhere in feed history,
since that jump cursor stays `created_at`-based. Unread *counting* and
`[N]ew scan`'s "has unread" detection are correct either way. Boards no
longer have this gap: issue #710's jump is computed from the unread
posts themselves (below).

**Boards: a post is read once it is opened (issue #710).** Showing a post
in the board list does not mark it read; opening it in the reader does. A
board cursor's `last_seen_arrival_id` is a **floor**: every post at or below
it, in arrival order, is read. `user_board_opened_posts` holds the posts a
user opened *above* the floor, one row per `(user, board, post row id)`.
Unread means above the floor and not in the set, and every surface that
reports unread uses that one rule: the list's `new` markers,
`unread_post_count` (`[N]ew scan`, the board picker) and
`unread_replies_to`.

The set stays small by construction:
- It only ever holds **out-of-order reads**. When the opened posts run
  unbroken up from the floor, the floor advances past them and their rows
  are deleted, so a caller reading in order never stores a row at all.
  "Unbroken" is over the posts a reader may see: a post pending approval,
  hidden by trust (§12) or deleted is not a gap anyone could read, so it does
  not hold the floor back.
- It is **capped** at 500 rows per user and board. Past the cap the floor
  moves up to the oldest kept row, and the oldest gaps are given up as read.
  Reaching the cap takes more than 500 posts opened while skipping others on
  one board.

`[M]ark all read` (on the list, and per board in `[N]ew scan`) moves the
floor to the newest visible post and drops the set. A post still pending
approval above it stays unread, so it is new when it appears. A caller's
own new post is recorded as opened when it is written.

The jump to the first unread post (`board_read_cursor`, used by `[N]ew
scan`) is computed rather than stored: the feed position just before the
oldest unread post by feed order, so an unread post below others already
opened is where the jump lands, and a late carried post is found wherever
its authored date puts it. With nothing unread it is the newest post, and
the jump shows the ordinary newest page. Both lookups stream newest or
oldest first and stop at the first visible row.

A trigger deletes a post's opened rows when the post is deleted, since
`posts.id` can be reused once the newest row is gone. The migration makes
existing cursors the floors, so nobody's history is reset. A legacy cursor
without an arrival id (issue #72's backfill found its post deleted) read by
feed position: its floor is set just below the first root past that
position, and the roots above the floor it had read become opened rows.

#### Follows and favourites

Follow state is a new, separate table — `(user_id, object_type, object_id)`
where `object_type` is `community`/`board`/`channel`/`file_area` — deliberately
independent of every existing access concept it sits beside:

- **not** channel membership/invitations (`netbbs.chat.membership`), which
  govern *whether you may enter*, never *whether you care about it*;
- **not** node carry policy (`netbbs.link.boards.materialize_carried_board`),
  which is a per-node, all-or-nothing decision about whether Linked content
  exists locally at all, made with no per-user awareness whatsoever today;
- **not** Community membership, since a Community has no membership concept
  to begin with — it is a browsing/navigation container, not a joined group.

Following an object a user can no longer read (level raised, Community/
channel access changed, or — for a Linked board — this node stopping carrying
it) is never actively revoked; it simply stops being resolvable and is
filtered out of every follows-aware view at display time, the same
lazy-filter approach category/board listings already use elsewhere for
resources no longer visible.

**Following, as a caller does it** (issue #675):
- **From `[N]ew scan`:** `[F]ollow` follows the highlighted board, channel or
  file area, or stops following it. `[V]iew followed` switches between only
  what the caller follows and everything; it is refused while nothing is
  followed. Followed rows are listed first and marked `*`.
- **From a resource's own screen:** a board's post list and a file area's
  screen each have a `[F]ollow`/`Un[f]ollow` toggle, and a chat channel has
  the `/follow` command, which toggles the same way.
- **Communities:** a Community is not followed yet. The table allows it, but
  nothing would show the difference: `[N]ew scan` has no Community rows, and
  a Community's boards, channels and areas are followed one by one.

#### Activity summary and direct jump ("new scan")

A single new main-menu entry — `[N]ew scan`, the traditional BBS term for
exactly this feature — is the fast, always-shown surface issue #56 asks
for, always shown like `[M]essage boards`, `[C]hat` and `[F]iles`.

New scan covers **every board, channel, and file area the user can currently
access**, not only followed ones — matching the traditional meaning of a
new-scan pass, and avoiding a chicken-and-egg problem where a brand-new
account has followed nothing yet and a "new scan" would show nothing at all.
Followed objects are surfaced first / distinguished within that same list; a
follows-only filtered view remains one keystroke away for a user who wants to
narrow it. Within new scan, a dedicated "replies to you" pass (described
above) runs across every board regardless of follow state, since a reply is
always worth surfacing.

Selecting an item from new scan jumps directly into that resource
pre-positioned at the first unread item — mechanically, calling the
resource's own existing keyset-pagination entry point with `after=` set to
the user's stored cursor, not a new navigation primitive. When that item is
on the newest page, the jump opens the newest page with the cursor on it
(issue #839): a page starting at the first unread left out every read post
and numbered the rest from 01, so the number a caller remembered from an
ordinary visit picked nothing. Only a caller with more unread than a page
holds gets the page that starts at the first one. `[/] Find` jumps the same
way.

New scan is a walk, not a one-shot list (issue #839). Back from a board,
channel or area opened from it comes back to it, reloaded in place, with the
cursor on the next row that has something waiting, and a line above the
prompt naming it, so Enter after Enter goes through everything new. A board
or area never visited counts as having something when it holds anything, and
its row says how much ("not yet visited, 3 posts") rather than only "not yet
visited". `[R]eplies` lists the replies to the caller's posts, one to a row;
picking one opens its board with the cursor on it.

Back from a board or file area opened from a list returns to that list, on
the row left, and Back from a category's list to the list above it (issue
#839). They used to return past the list, to whichever menu had opened it.

#### Local search

Implemented (issue #56's last piece). Local search is a new, separate
capability from the item picker's simple, per-call substring name match
(`pick_item`'s own search command, unrelated and unchanged — see below):

- **scope**: only this node's own already-stored content — approved board
  posts (subject/body), approved file entries (filename/description),
  retained channel scrollback (message body), and the searching caller's own
  mail (issue #824, below). Never content this node does not itself carry —
  there is no Link-wide query protocol, and this design does not imply or
  require one;
- **mechanism**: SQLite FTS5 virtual tables (`post_search`, `file_search`,
  `channel_message_search`), kept in sync with `posts`/`files`/
  `channel_messages` by explicit calls from `netbbs.boards.posts`/
  `netbbs.files.entries`/`netbbs.chat.scrollback` at every write path
  (create/edit/approve/delete/expire/trim) — deliberately not SQL
  triggers, matching this schema's existing convention of zero triggers
  anywhere else and keeping the sync logic visible in Python. `post_search`
  holds only the *resolved current* approved revision of a post's edit
  chain (mirroring `_resolve_current_version`'s own "newest approved row
  for this root" query) — a superseded revision, a still-pending edit, or
  a root with no approved revision left is never indexed.
  `channel_message_search` is pruned in the same statement that trims
  scrollback's own ring buffer, so a search can never surface a message
  already gone from retained scrollback. `mail_search` mirrors
  `mail_messages` one-to-one, keyed by the letter's id as its rowid, and
  is written and removed in the same transaction as the row
  (`netbbs.mail`, `netbbs.link.mail`);
  FTS5 availability was traced, not just assumed, for this project's actual
  NetBSD/pkgsrc target: `lang/python312`'s Makefile buildlinks against
  `databases/sqlite3` (not an amalgamation bundled into Python itself), and
  that package's own Makefile passes `--fts5` unconditionally in
  `CONFIGURE_ARGS` — so pkgsrc's Python `sqlite3` module should always have
  it. A build lacking it fails the schema migration loudly
  (`sqlite3.OperationalError: no such module: fts5`) rather than degrading
  silently, consistent with this project's "fail clearly" convention;
- **authorization**: a search result set passes through the exact same
  visibility rules (level/age/Community gates for boards and file areas,
  `netbbs.net.chat_flow.list_visible_channels_for` for channels) normal
  browsing already enforces — search can never be a side-channel that
  reveals a restricted resource's existence or content;
- **privacy, explicit**: a user's search query text is never transmitted to
  any peer or broadcast over Link, by default and without exception in this
  design. Searching a Linked board only ever searches this node's own
  locally carried copy of it. A future Link-wide search capability, if ever
  built, is a distinct protocol extension requiring its own explicit design
  (rate limits, query exposure, opt-in) — never an implied consequence of
  local search existing;
- **UI**: a new, always-shown `[/] Find` main-menu entry (`netbbs.net.
  scan_and_find._find_screen`), alongside `[N]ew scan` — prompts for one
  free-text query, matches it against all three content types at once, and
  jumps straight to a selected hit: a post/file lands on the exact matched
  item (`netbbs.search.post_jump_cursor`/`file_jump_cursor` compute the
  `after=` cursor that makes it the first item shown, reusing the same
  `initial_cursor` parameter `[N]ew scan` already threads through
  board/file-area viewing) rather than just opening its board/area at the
  default newest page. A channel message instead just enters its channel —
  channels have no "jump to one message" concept, the same limitation
  `[N]ew scan`'s own channel dispatch already accepts.
- **the caller's own mail (issue #824)**: Find searches the caller's Inbox
  and Sent folder, never anyone else's mail and never a letter the caller
  deleted from their side (the other party's copy stays theirs). System
  mail is in the Inbox, so it is searched too. A letter matches when each
  typed word is in its subject, its body as plain text (color codes and
  escapes removed, as the post index does), or the From/To name the mailbox
  shows. Those names are resolved when shown — a Link node can rename, a
  local recipient is looked up by id — so they are matched at query time,
  word by word the way FTS5's `unicode61` tokenizer matches, rather than
  indexed; the subject and body come from `mail_search`. Mail results are
  listed first, ahead of posts, files and chat (issue #918: the caller's
  own mail is often what they are looking for), under `[MAIL]`, "from" or
  "to" the name, newest first, capped at 20 like every other kind, with the
  same "top 20 per category" notice when more matched. The main menu's
  Find entry and the Find screen's subtitle name mail first too ("Search
  mail, posts, files, chat"), in the order the results come. Nobody mail is
  closed to (issue #816: the guest, including a session that came in as the
  guest, and callers below the mail level) gets mail results, and the main menu's
  Find entry then names only posts, files and chat. A letter opens in the
  mailbox's own message view with all its actions, and opening an Inbox
  letter marks it read as in the mailbox; `[B]ack` returns to the results,
  and a letter deleted there leaves them. The gate is checked again when a
  letter is opened, since the SysOp can close mail meanwhile.

  An index rather than a scan of the caller's rows: the Inbox is capped at
  500 but each letter may be 20 KB, Sent is not capped, and the scan runs
  on the node's single database lane, so a mailbox full of long letters
  would have held every other caller's queries for seconds. The index is
  one more copy of private text, which is why its entries go with the row
  and why the integrity check covers it.

Local, in-page substring matching over a short list (`pick_item`'s own
search command) is unrelated and unchanged — it is not "search" in this
section's sense, just incremental filtering of an already-open, already
access-checked list.

**Integrity checking and rebuild (issue #74).** Because the four FTS
tables above are synced by explicit per-write-path calls rather than one
shared transaction with the authoritative write, a crash between the two,
a future write path that forgets to call the right reindex function, or a
restored older backup can leave them stale with no prior way to detect or
repair it. `netbbs.search.check_index_integrity(db)` reports drift
(missing/stale/extra entries, by id only — never the drifted content
itself) for all four tables against authoritative `posts`/`files`/
`channel_messages`/`mail_messages` data; `netbbs.search.rebuild_indexes(db)` replaces
their contents outright, using the exact same "what should be indexed"
computation the check compares against, so a rebuild always converges to
a clean check immediately after. Exposed as a standalone maintenance
command, `python -m netbbs.search check|rebuild --db PATH`, mirroring
`python -m netbbs.backup`'s own subcommand shape.

**Explicit decision: startup detects nothing automatically.** Unlike
`Database.check_integrity`'s `PRAGMA integrity_check` (a full-database
scan run once at every node startup, §13's startup/crash-recovery
rules), FTS drift checking is *not* wired into node startup. The
database-corruption check is cheap relative to node startup and guards
against a failure mode (disk-level corruption) that can occur at any
time regardless of how careful this codebase's own write paths are; FTS
drift is a narrower, rarer failure (a missed reindex call, a crash in
one specific window) whose check cost scales with indexed content
rather than staying close to constant. Treat it as an operator-run
maintenance action for now — after a crash, an interrupted migration,
or a restored backup — rather than a mandatory gate on every start.
Wiring a summary into the `[D]iagnostic log`/SysOp status surface is
possible future follow-up, not required by this decision.

### 6.7 Self-update

Release discovery uses explicit GitHub Releases over HTTPS rather than an
arbitrary branch HEAD. NetBBS checks once at startup and then daily by default;
a SysOp can also check immediately from `[S]ettings` -> `[U]pdate`. Automatic
checks only record their result. They do not download, install, or restart
NetBBS, and they do not send an unsolicited in-session notification. The result
is visible on the SysOp dashboard and update-settings screen. The
operator-visible switch controls the startup/daily checks only; manual checks
remain available when it is off.

**Installing from the console (issue #731).** When the last check found a newer
release, a SysOp on the live node can press `[I]nstall vX` on the Update screen.
The screen shows the plan first and does nothing until `[I]nstall now` and a
final yes. The steps run in order, and each one that fails stops the rest and
records why:

1. **Download.** The node asks the release API for that tag and takes the one
   asset named `netbbs-<version>-py3-none-any.whl`. It refuses a draft or
   pre-release, an asset whose address is not under the project's
   `releases/download/<tag>/`, an implausible size, and an asset without a
   published `sha256:` digest. The download is HTTPS end to end, redirects
   included. It is bounded by the announced size (64 MiB at most) and 300
   seconds. It is written under a temporary name and kept, beside the
   database in `<db-stem>_updates/`, only when its SHA-256 matches the digest.
2. **Back up.** A complete live backup, exactly as Backup's Create does. A
   refused backup, for example with a War Dialer world in use, stops the
   install.
3. **Install.** `pip install` of the verified wheel into the interpreter the
   node runs on, with the extras this installation has. An extra counts when
   every requirement it names is installed; `dev` never counts. pip resolves
   dependencies as usual, so a release that needs a newer dependency fetches
   it from the package index. The node refuses to install:
   - outside a virtual environment;
   - from an editable or VCS install;
   - where the service account cannot write the environment.

   pip runs as an owned subprocess, bounded at 900 seconds, and only the tail
   of its output is kept, for the failure screen. A fresh interpreter then
   reports the installed version. Anything other than the target counts as a
   failed install.
4. **Restart.** The node records the install, then does one of two things:
   - It shuts down gracefully: callers are warned and the configured delay
     applies. It then exits with status 75, so the service manager starts the
     new build.
   - It stops there and tells the SysOp to restart the service.

   Which one is the SysOp's declaration under `[R]estart after install`. The
   setting is `auto` by default, which trusts detection: systemd's
   `INVOCATION_ID` counts as a supervisor that restarts NetBBS, and NetBSD
   rc.d, which does not restart a stopped node, is not detected. The request
   for status 75 holds only while that restart shutdown is the one in
   charge. A SysOp who cancels it, or a SIGTERM that replaces it, withdraws
   it, because a service manager stopping the node must leave it stopped.
   The shipped systemd unit restarts on 75 under `Restart=on-failure`, and
   also lists 75 under `RestartForceExitStatus=` and `SuccessExitStatus=`.

The next start compares the version it runs with the recorded target and
records the outcome for the Update screen: "installed vX and restarted into
it", or "installed vX, but this node started as vY".

Between the install and the restart, the old process keeps running with the
new files on disk. A module imported for the first time in that window would
come from the new release. That is why the restart path follows the install
immediately, and why the no-restart outcome says plainly to restart now.

**Not automated:** rolling back. Going back means reinstalling the previous
release's wheel and then restoring the pre-upgrade backup the install just
made, so that code and schema roll back together. The operator-driven
procedure remains fully supported: back up, stop the service, install the
release wheel into the same environment with the same extras, start.

`netbbs.selfupdate` also still contains tarball extraction, database snapshot
and pending/confirm/rollback primitives from an earlier re-exec design. No
command, menu or lifecycle path calls them.

HTTPS and GitHub are the update trust boundary. The digest the install checks
comes from the same release API over the same TLS. It proves the bytes are the
ones GitHub holds for that asset, so a truncated, corrupted or swapped download
fails. It does not prove that a maintainer signed them. An asset without a
digest is refused rather than installed on TLS alone. Release signing remains
a possible hardening step and is not required by the present design. GitHub
computes asset digests on upload, so the release recipe needs no extra step;
it must attach the wheel under its standard name.

GitHub Releases and tagged source are the only official NetBBS distribution
and update channel. `pip` is used to install an official release wheel; it is
not an independently maintained source of NetBBS packages. External package
managers may supply dependencies, but a PyPI, pkgsrc, apt, or other independently
managed NetBBS package is not supported because it would introduce a second
owner and release channel for the installed application files.

---

## 7. NetBBS Link: identity, events, and compatibility

### 7.1 Phase-3 safety boundary

Phase 3 is for private, controlled federation. It may use local blocklists as an
interim abuse control, but it is not safe to expose broadly to unknown peers
until Phase 4 defines and implements trust, probation, reputation, and
quarantine.

### 7.2 Canonical event envelope

A durable Link event uses a signed envelope:

```json
{
  "netbbs_protocol": 1,
  "object_type": "...",
  "payload": { ... }
}
```

The content ID and signature cover the entire canonical envelope, including the
object type. Object type is therefore **mandatory domain separation**: it is
intrinsic to the exact bytes that get hashed and signed, not a caller
convention a future event type could accidentally bypass by reusing another
type's shape.

**Canonicalization rule** (binding and language-independent — issue #11):

- Compact JSON: no insignificant whitespace, `":"`/`","` separators only.
- Serialize using ASCII escapes for non-ASCII characters, matching the
  reference canonicalizer's `ensure_ascii=True`, then encode as UTF-8.
  Supplementary characters use JSON surrogate-pair escapes. Equivalent
  unescaped Unicode JSON is not the same signed byte representation.
- Object keys sorted by exact Unicode codepoint sequence, at every nesting
  depth, after normalization (below).
- Every string is recursively normalized to Unicode NFC before serialization —
  object member names as well as values, at every nesting depth, not values
  alone. Two payloads differing only in normalization form (precomposed versus
  combining-mark sequences) canonicalize identically and share one content ID,
  whether the difference is in a value or in a key. Two distinct source keys
  that would normalize to the same string are a normalization collision and
  are rejected outright, the same way a duplicate wire key is (below) — never
  silently resolved by whichever one happens to overwrite the other.
- Floating-point values are forbidden anywhere in a hashed or signed field:
  float serialization is not reliably deterministic across languages and
  platforms.
- Any other JSON number is an integer and must fall within
  `[-(2^53 - 1), 2^53 - 1]` — the widest range exactly representable as an
  IEEE-754 double, matching JavaScript's/JSON's own safe-integer bound. No
  current field approaches this bound; the rule exists so a future field
  cannot silently produce bytes only an arbitrary-precision-integer language
  can hash consistently.
- `true`/`false` are booleans, never conflated with the integers `1`/`0`.
- A field that does not apply to a given event omits the key entirely.
  Storing it as an explicit JSON `null` is a **different, distinct canonical
  value** — `{"parent_post_id": null}` and `{}` must never share a content ID.
  Each event schema states, field by field, which behavior applies; a builder
  must not choose between omission and `null` ad hoc.
- Wire JSON containing the same key twice in one object, at any nesting
  depth, is rejected outright before it is canonicalized, hashed, or
  verified — never silently resolved by a "last one wins" rule. Two
  different JSON parser implementations can disagree about which duplicate
  value wins; a sender and receiver that disagree would each reconstruct a
  different object from what they would both call "the same bytes."

`netbbs.boards.content_id.canonical_json_bytes` is the sole canonicalization
implementation this codebase uses to produce these bytes. Anything that
signs, verifies, or content-addresses a Link event reuses it directly, never
a second, independently-maintained implementation that could quietly drift.
`netbbs.link.events.strict_json_loads` is the reference implementation of the
duplicate-key rule, applied to every message this node's transport reads off
the wire before that JSON becomes a candidate envelope.

Golden test vectors (`tests/fixtures/link_canonical_vectors.json`, checked by
`tests/test_link_canonical_vectors.py`) pin exact canonical bytes and content
IDs for representative payloads, including Unicode normalization,
omitted-versus-null, and integer-boundary cases. An independent implementation
must reproduce them exactly for canonical-format compatibility. This is
necessary but not sufficient for full Link interoperability: identity,
authority, transport, lifecycle, and trust behavior also require validation.

Existing Python behavior implements the rule above; it is not a separate,
looser specification of its own.

### 7.3 Author references

An event author is a tagged union:

- `node_vouched_user`;
- `user_key`;
- `node`.

The verifier resolves the appropriate current signing key and, for node-owned
keys, validates its transition history back to the root identity.

Only the author tiers implemented for a specific event type are accepted. The
existence of the tagged union does not imply every tier already works for every
feature.

A `node_vouched_user` author (or a `link_message` sender/recipient) is
identified by the pair `(home_node_fingerprint, local_user_id)`, never by
`local_user_id` alone — a username is unique only within its own node, so the
pair, not the bare name, is the globally-scoped identity issue #11 asks for,
matching the `user@node-fingerprint` addressing form already used elsewhere.
`local_user_id` is the account's canonical, immutable, stored-case username
(§5); it participates in canonical bytes exactly as stored, after the same
NFC normalization every string field receives — never case-folded the way
local login/uniqueness lookups are, since a signed event fixes one exact
string forever, not a case-insensitive equivalence class.

### 7.4 Immutable content and state-changing chains

There are two event classes.

#### Immutable creation events

Examples include a board post or file descriptor. Their content ID identifies
the complete immutable object. Nodes may differ only in whether they possess or
locally suppress it.

A random nonce distinguishes two intentional posting actions with otherwise
identical visible content.

#### Per-object state chains

Edits, metadata changes, grants/revocations, key transitions, origin transfer,
closure, and membership changes extend a per-object chain. Each new event
references the state/event it extends.

Effective state is the projection/fold of the valid chain. An incoming event is:

- a valid extension of the current state;
- an already integrated ancestor, therefore an idempotent no-op;
- a genuine competing extension/fork requiring the object’s defined policy.

Transport deduplication is only a performance optimization. Permanent replay
safety comes from the authoritative object state or chain, not from a purgeable
“seen ID” cache.

Tombstones are chain events, not deletion of history. Local byte pruning cannot
resurrect state if the permanent projection rules remain intact.

Two events both validly extending the same predecessor at the same instant is
impossible by definition: a chain has exactly one current head, and an
incoming event either extends it (accepted) or does not (rejected as
reordering, or handled as a fork, per the object's own policy). `created_at`
is descriptive metadata for display and audit, never the mechanism that
orders or authorizes a chain extension — two genuinely successive events can
legitimately share one clock's timestamp resolution. Reconstructing a chain
from storage (for example, after a restart) must walk the same
`previous_event_id`/head-pointer links original acceptance already verified,
or rely on the storage layer's own locally-assigned, monotonic receipt
ordering — never re-sort on the payload's own claimed `created_at` alone.
Ordering among unrelated immutable events for local presentation (for
example, a board's post listing) is a separate, local concern with its own
stable tie-break, not a protocol question.

### 7.5 Version and unknown-event behavior

`netbbs_protocol` changes only for incompatible wire semantics. Additive event
types or optional fields need not force a protocol bump when old peers can
safely preserve them.

Peers exchange supported protocol information during authenticated contact.
Unknown event types or unsupported versions may be stored and relayed opaquely,
but must not be projected, displayed, or treated as authority by a node which
cannot interpret them.

**How a node keeps an unknown event type** (issue #1022). An event of a type
the node does not understand is taken with the rest of the batch it came in,
never refused for its type alone, and kept apart from the events the node
has verified (`opaque_events`).

- **What is checked.** Only its shape (a well-formed type name, a payload
  object, a signature string) and its size, at most 64 KiB. Its signature
  cannot be checked, because who must sign depends on the type.
- **Bounds.** At most 500 per sending peer, that peer's oldest going first,
  and none kept past 90 days. An event dropped by the bound is forgotten, so
  it can be taken again later.
- **Relay.** When its payload names a `board_id` this node carries, it is
  declared and served in that board's inventory like any board event, so
  peers pull it. Otherwise it is kept but not passed on. A node never pushes
  what it could not verify.
- **After an update.** At startup, each kept event whose type the node now
  understands goes through the normal checks as if just received from the
  peer that sent it: valid ones are accepted and projected, invalid ones and
  ones the checks pass over (a setting from a board's former origin) dropped,
  and ones waiting for something the node lacks (their signer, what they
  build on) kept for the next start.

A new event type therefore travels through nodes that do not understand it
yet, and takes effect on each of them once it updates.

Unknown fields within a known signed event must be preserved in the original
signed representation. A node must not strip and reserialize them in a way
which changes the signed bytes.

---

## 8. NetBBS Link transport, discovery, and distribution

### 8.1 Traffic-family split

Asynchronous/store-and-forward features use signed HTTP+JSON:

- key and endpoint state;
- boards and Link messages;
- future file catalogues and chunk requests;
- governance events.

Real-time Link chat will use a persistent mutually authenticated Noise channel
with the node transport key. Do not force asynchronous and real-time traffic
through one protocol merely for uniformity.

### 8.2 Hello and endpoint state

A hello is self-authenticating and carries enough root and transition state to
resolve the current signing key, plus a signed endpoint descriptor.

Endpoint descriptors may advertise ordered addresses and relay information.
The newest valid descriptor wins; stale repeats are harmless.

A descriptor also lists `capabilities`: the optional Link behaviours the
signing node's code understands (issue #669). A peer uses such a behaviour only
with a node that advertises it; a descriptor without the list advertises
nothing. It describes the software, not a setting, so every descriptor a given
version signs carries the same list. Two exist: `inventory_not_carried` (issue
#669) and `inventory_pages` (issue #685), both for signed `InventoryRequest`
fields (§8.8).

A descriptor may also carry `dial_in`: up to four URLs at which a *caller*
reaches the signing board, as opposed to the Link `addresses` a node dials
(issue #767, §8.12). Each is `telnet://host:port`, `ssh://host:port` or an
`https://` URL, at most 300 bytes. It is the SysOp's own statement, since no
node can see the port-forward or proxy in front of its listeners. A node that has
never stated it publishes `[web] public_url` when that is an `https://` URL
within the same 300 bytes,
the one caller-facing address a SysOp has already declared, or nothing. The field is display-only: a reader
drops a malformed entry and a malformed list reads as empty, as for
`live_relays`, and never refuses the hello over it. An older node keeps and
forwards the field inside the signed envelope without reading it.

The protocol logic remains transport-independent. The `aiohttp` adapter is the
boundary translating protocol messages to real HTTP requests and responses.

### 8.3 Bootstrap and peer discovery

Bootstrap sources are combined, not exclusive:

1. operator-configured seeds;
2. the software-shipped reliable-nodes fallback (Reliable Link first; §16,
   issue #219);
3. the live reliable-nodes roster, fetched daily from
   `https://www.netbbs.org/reliable-nodes.json` and preferred over the
   fallback once any fetch has succeeded -- one list serving default seeds,
   asynchronous relay candidates, and the live-relay anchors (§8.10.3), and
   dialed only after the SysOp accepts reliable-node participation;
4. signed/verified peer-list exchange after contact;
5. bounded fallback attempts to discovered candidates when normal seeds fail.

Seed or peer introduction never implies trust. Identity verification is
cryptographic and independent of the network address which introduced a peer.

A compromised bootstrap source can attempt an eclipse or steer connection
attempts, but cannot impersonate an existing node without its key.

### 8.4 Full and outgoing-only nodes

A full peer advertises reachable addresses and accepts inbound Link traffic.
An outgoing-only node initiates connections but cannot be dialed directly.

Multiple addresses are tried in order. Simultaneous HTTP dials require no
connection-role tiebreak because they are independent idempotent request/
response exchanges, not competing persistent sessions.

### 8.5 Relay service for outgoing-only nodes

Outgoing-only nodes select a small redundant set of reachable full peers based
on direct-observation reliability. Relay participation requires signed consent.
A node may opt out of serving relays and may cap the clients/resources it serves.

Accepted relays are published through endpoint state and replaced when observed
reliability degrades.

The relay mailbox currently supports opaque encrypted Link-message envelopes:

- relays see routing metadata and size, not message content;
- storage is bounded in number and in time (below);
- pickup authenticates the intended recipient;
- the recipient re-runs normal event verification rather than trusting the
  relay’s claim;
- relaying does not introduce strangers or weaken the rule that sender and
  recipient identities must already be known sufficiently to verify and
  encrypt.

A relay holds at most `MAX_MAILBOX_ENVELOPES_PER_RECIPIENT` (50) envelopes
per recipient, refusing a further deposit with HTTP 507, and keeps each for
at most `RELAY_MAILBOX_RETENTION_DAYS` (30 days) from its deposit (issue
#891). Every sync pass drops what has waited longer, acknowledgements
(`link_message_accepted`/`_bounced`) as well as letters, and logs a WARNING
naming each recipient and how many went. Without the time limit, a recipient
that never came back -- retired, reinstalled under a new key, gone -- kept
its 50 slots forever and every later letter for it was refused.

Dropping is silent toward both ends. The relay can neither read nor sign
anything for the recipient, so it cannot bounce, and nothing announces the
retention on the wire. The sender learns of the loss from its own timeout on
relay handoffs: a letter handed to a relay expires on the sending side 14
days after the handoff (issue #874), with a notice saying no answer came
back. The relay's retention must therefore stay comfortably longer than that
timeout, so that a letter the sender is still waiting on is never the one
dropped. For that reason it is a constant rather than a SysOp setting: a
relay configured to keep mail a week would turn the sender's "may not have
arrived" into "certainly did not". The SysOp's **Link status** relay section
lists each recipient held for, with its count and the age of its oldest
deposit, oldest first.

A relay also holds sealed attestation bundles (issue #632) for the nodes it
relays for, in a table of their own: one slot per (issuer, recipient), a newer
bundle replacing an older one, bundles from at most 32 issuers per recipient,
each kept at most 90 days. They never take one of a recipient's mail slots.
Pickup hands them over beside the mail, in a `bundles` key an older recipient
ignores; an issuer deposits one only at a relay whose descriptor advertises
`sealed_attestations`. An issuer that is itself one of the recipient's relays
-- the usual shape, an outgoing-only node relayed by the reachable node it
dials -- puts the bundle straight into its own slot (issue #1046). A bundle is deposited with its issuer's hello bundle;
the relay accepts it only if its outer signature verifies under the issuer's
current key as that hello (merged with any chain on file) establishes it, so
only the issuer can fill or replace its slot.

Reliability scoring is direct-observation operational data, not Phase-4 social
reputation.

### 8.6 Current synchronization model

Current background sync:

- contacts configured/cached seeds and candidates;
- performs hello/peer discovery;
- pushes locally originated events the peer has said it lacks, plus
  `key_transition`s unconditionally (§8.8's *Push direction*, issue #478);
- relies on idempotent acceptance;
- sends targeted Link mail directly or through a selected relay;
- requests and applies bounded inventory/pull-based catch-up for linked
  boards, channels, and file-area catalogues from every seed dialed that
  pass. Responders include carried resources absent from the request, so
  an empty inventory can discover a first resource through a carrier
  (§8.8, issues #85/#94).

This is intentionally simple but incomplete.

Not yet present:

- efficient per-peer deltas beyond a full per-board known-ID list (fine at
  this project's declared scale; a compact digest would be needed beyond it) —
  this bounds the size of one inventory request, and therefore of the push it
  provokes, in *both* directions;
- complete retained-event and dedup-purge policy — `key_transition` alone
  is purged (§8.9, issue #86, closed); every board-scoped type stays
  unbounded, stated explicitly as still-needed, not silently deferred;
- public-network backpressure and abuse handling.

### 8.7 Store-and-forward goal

The eventual model supports nodes which are offline for extended periods and
resume synchronization later. Causal relationships come from parent/chain
references; timestamps are secondary ordering data, and content IDs provide a
deterministic final tiebreak for truly concurrent siblings.

Persistent dedup uses exact IDs, not Bloom filters. False-positive data loss is
unacceptable. Retention cleanup must never turn an old state-changing event into
something re-applicable.

### 8.8 Inventory/pull-based catch-up and multi-hop relay (issue #85)

§8.6 named two concrete gaps in the push-only, direct-pairwise sync model:
no pull-based catch-up, and no multi-hop propagation (a node carrying
Alice's board events never relays them to Carol). This section specifies
both, deliberately reusing existing machinery wherever possible rather than
adding new protocol-verification surface.

**Scope.** Signed board-scoped events, linked-channel genesis/messages, and
linked file-area catalogue metadata are included. File bytes are fetched
separately and never appear in inventory. Identity (`key_transition`) events are
already gossiped to every configured seed every pass regardless (§12) and
are small enough that this has never been the gap; Link messages are
point-to-point by design (§10) and are explicitly excluded from any
multi-hop relay, matching their existing "no relay from a stranger" routing
boundary (§10.4) — nothing here changes how `link_message` is delivered.

**`InventoryRequest` — signed, destination-bound, fresh, and not a canonical
event (revised by issues #106/#124; originally shipped unsigned).** This is a bookkeeping request about
what the requester already has, not durable authored content, so it still
needs no content-addressing or gossip-replay semantics of its own — no
chain and no `content_id`. It **is** always signed by the requester's own
current operational signing key, the same "always signed by the
requester's own current key" shape §12's `relay_consent_request` already
established. Before Link v1 interoperability is frozen, issue #124 makes the
additional fields below required rather than preserving the replayable
pre-freeze request shape.

```
InventoryRequest {
  requester_fingerprint: string,
  responder_fingerprint: string,
  created_at: timestamp,
  nonce: 128-bit random hex string,
  signature: bytes,
  boards: { board_id: [known_content_id, ...], ... },
  channels: { channel_id: [known_content_id, ...], ... },
  file_areas: { area_id: [known_content_id, ...], ... },
  not_carried?: { kind: [resource_id, ...], ... },   // issue #669
  page?: { index: int, count: int }                  // issue #685
}
```

The signature covers every field except `signature` itself. A responder
requires `responder_fingerprint` to equal its own root fingerprint, rejects
timestamps more than five minutes old or ahead of its clock, and rejects a
recently seen `(requester_fingerprint, nonce)` pair. The replay cache is
process-local and capped at 4,096 entries; timestamp freshness preserves the
bounded replay window across restart. This prevents a captured request from
being redirected to enumerate a different peer or replayed indefinitely.

`boards` is keyed by every `board_id` the requester itself currently
carries (bounded by its own `max_carried_boards` quota, §13.9) mapped
to that board's full set of content IDs the requester already has for it.
`channels`/`file_areas` (§9.6, §11) are the identical shape for linked
channels and linked file-area catalogues respectively.

**A declaration too large for one request is sent in pages (issue #685).** The
quotas bound the keys, not the history under them. At about 30,000 held content
IDs the request outgrew the responder's 2 MiB `client_max_size` and was refused
with 413 on every pass, so pull, and the `wanted` push that rides on it,
stopped for good. When the IDs (and `not_carried`, below) would exceed 1 MiB,
half the limit, the requester splits them into `count` pages by
`inventory_page(id, count, nonce)` — the first eight bytes of the SHA-256 of
the request's nonce and the ID, big endian, modulo `count` — and each request
carries one page. The nonce is random, signed and fresh, so the split is new
every request: unsalted, a peer that authors events could vary them until
their IDs shared one page and push that page past the limit for good. Every key is still present; each list holds only its IDs on that
page, and `not_carried` only the resources on it. The signed `page` field tells
the responder which page it has, and the responder narrows its answer to match:
for a declared resource it compares only its own events on that page, and an
undeclared resource it answers for only when the resource ID itself is on that
page, because only there is the requester's `not_carried` complete and "absent"
still means "never seen". Any given item is on the page sent with chance
1/`count`, so a node past the limit catches up in about `count` passes rather
than never.
`count` is at most 4,096. Rejected: a per-resource digest with a full list
where it differs (a second round trip, and one very large resource, which a
busy linked channel becomes since channel events are never pruned, still
outgrows the body); a per-resource cursor or high-water mark (content IDs
carry no order the two sides share, and a gap below the mark would never be
asked for again); paging whole resources (the same single-resource ceiling).
Splitting within a resource lets a reply or edit arrive before what it refers
to, which the materializers already tolerate: gossip arrives out of order anyway.

`page` is signed and omitted for a one-page request, and it is sent only to a
responder whose descriptor advertises `inventory_pages` (§8.2), for the same
reason as `not_carried` below. An older responder is sent the same slice
without being told. It takes the rest as missing and answers with events the
requester already holds, which past its 200-event page can fill every response,
so pull from it may make no progress until it upgrades; the push half still
works, since `wanted` is computed from what the page declares. Nothing an older
responder understands can do better, and the whole declaration moved nothing in
either direction.

**Route: `POST {LINK_PATH_PREFIX}/inventory/{fingerprint}`**, mirroring
`/events/{fingerprint}`'s existing convention (`fingerprint` names the
requester — and, since issue #106, is now actually checked: see below).
The response is **not** a new envelope type either — it is the same raw
JSON event-list shape `push_events`'s request body already uses:

```
{ "events": [ <raw event dict>, ... ], "more_available": bool,
  "wanted": [ content_id, ... ] }
```

`wanted` is the push direction's half of the same exchange (issue #478,
below). It is required: a response omitting it is malformed.

**Responder-side diff.** For each `board_id` this responder itself
currently carries — whether or not it appears as a key in the request at
all (see the discovery paragraph below) — return every board-scoped event
on file for that board whose `content_id` is not in the requester's
declared list for it (an absent key is treated as an empty declared
list). The diff unions three differently-shaped sources, not `link_events`
alone: this node's own self-originated genesis/lifecycle (`boards.
link_genesis_json`/`link_lifecycle_json`, never routed through
`handle_events` at all, so never in `link_events`), any post/edit a
*local* user authored on any Linked board regardless of whether this node
originated or merely carries it (`posts.link_event_json`, populated only
by self-authorship, per `netbbs.link.boards.queue_board_post_if_linked`'s
own scope), and every peer-received event this node has accepted
(`link_events`, filtered by the new `board_id` column — see the schema
change below). A `board_id` the responder does not itself carry is
silently skipped, never an error — "not carrying this board" is already a
legitimate, honestly-represented answer (§9.3). **This is the entire
multi-hop mechanism**: a node that only *carries* board X (never
originated it) can now answer an inventory request for X from a third
node, because the diff draws from everything this node has on file for
that board, not only what it originated.

**Empty-request discovery (issue #94) and its authentication precondition
(issue #106).** The diff above answers for every `board_id`/`channel_id`/
`area_id` the *responder* carries, not only ones present as keys in the
request — so a requester with nothing carried yet can send an entirely
empty `InventoryRequest` and still discover its first Linked board/
channel/file area, rather than being stuck needing to already know an ID
it has no way to learn. Before this discovery behavior existed, the lack
of any authentication on this route cost nothing: an arbitrary caller
still had to already know a specific ID to ask about. Once an empty
request could return *everything* a node carries, the same unauthenticated
route became a resource-enumeration/content-disclosure endpoint for
anyone on the network, not just configured/verified Link peers. `LinkNode.
handle_inventory_request` therefore requires, before any diff logic runs:
`fingerprint` (the URL path segment) must already be a completed peer (the
same "no pull from a stranger" boundary `handle_events`/`handle_peer_list`
already enforce); the signed `requester_fingerprint` inside the request
must equal that same `fingerprint` (a completed peer cannot enumerate on
some *other* peer's behalf); and `signature` must verify against that
peer's current resolved signing key — proving current possession of the
identity, not merely a previously-observed, publicly-discoverable
fingerprint (fingerprints are exactly that: discoverable via the
deliberately-unauthenticated `/peers` route, §8.3). The signed responder,
freshness, and nonce checks above then prevent cross-responder reuse and
replay. The governing
invariant: a completed, cryptographically verified peer may send an empty
inventory and discover everything this node carries; an arbitrary
unauthenticated HTTP client may not enumerate anything. A request failing
any of these checks is refused outright (HTTP 403) — there is no
degraded/partial-answer tier.

**`handle_events` itself needs one correctness fix to make this
verifiable, not zero changes.** Every board-scoped branch (`board_genesis`,
`board_post`, `board_post_edit`, `board_origin_transfer_offer`/
`_accepted`) previously required the wire-level `sender_fingerprint` to
*equal* the content's own claimed origin/author, resolving the signing key
to verify against from `self.peers[sender_fingerprint]` — correct for
direct delivery, but structurally incompatible with relay: a genuinely
relayed event's wire sender (the carrier) is a different node than its
signed author/origin, so requiring equality made multi-hop content
unconditionally unverifiable, not merely unsupported. The fix resolves each
branch's signing key against the **content's own claimed origin/author
fingerprint** (already present in its payload) instead of the wire sender —
but only if that origin/author fingerprint is *itself* already a peer this
node has independently completed a hello with (`self.peers.get(...)`,
raising the same `LinkProtocolError` "no relay from a stranger" shape
otherwise). This preserves the exact same safety property in spirit —
nothing is ever accepted whose signing key this node can't independently
verify via its own previously-established trust — while correctly relocating
*which* fingerprint that trust check applies to: the content's author, not
whoever happened to relay the bytes. The wire-level `sender_fingerprint`
must still itself be a completed peer (unchanged, checked at the top of
`handle_events` as before) — relay only ever happens between two nodes that
have completed a hello with each other. The *author* of what is relayed need
not have: see §8.11. `key_transition` and the `link_message` family are explicitly
untouched — messages remain point-to-point by design (§10) and were never
part of this issue's scope.

**Applying the response needs no *new* acceptance path beyond that fix.**
The requester feeds the returned `events` list through the now-corrected
`LinkNode.handle_events` exactly as it already does for a push response —
chain/dedup logic and materialization are otherwise unchanged. The new
implementation surface is (a) the `handle_events` fix above, (b) the
responder's three-source diff query, and (c) a client-side loop that issues
the request and applies the response.

**Who signed it has to be verifiable, and a hello is not the only way to
learn that (issue #630, §8.11).** Multi-hop propagates *content* through an
intermediary; it does not substitute for the receiving node's own
verification of who signed it. Requiring a completed hello with the author
or origin is not an option: a node dials its seeds and turns to candidates
(§8.3) only when they fail, onboarding gives every new node the same seed,
and two outgoing-only nodes cannot dial each other at all, so two nodes that
share a board through a common seed have, as a rule, never met. The
receiving node learns such an author's identity from the carrier, as §8.11
describes, and verifies the content against that. A node whose identity it
cannot learn cannot have its content accepted, and an event in that position
is set aside without costing the rest of the response.

**Empty inventory is discovery, not "ask about nothing" (issue #94).**
Although each request dictionary lists what the requester currently
carries, the responder walks the union of requested and locally carried
IDs. A missing key therefore means "the requester has never seen this,"
and the responder returns that resource's events subject to the same
authentication, origin-verification, quota, and response-size boundaries.
This lets a node discover its first board/channel/file-area catalogue
through a carrier without weakening the independent-known-origin rule
above.

**Bounded response size.** Capped at the existing `_MAX_EVENTS_PER_REQUEST`
(200, §13.9) — the same constant `handle_events` already enforces on the
receiving end, not a new number. If more than that many events are missing
for the requested boards, `more_available` is `true` and the requester
simply asks again next pass; because its own `known_content_id` list for
each board grows after every partial response, each subsequent pass
naturally asks for a shrinking remainder — no separate pagination cursor is
needed.

**Requester side (`netbbs.link.sync`).** Each pass, every seed whose hello
completed receives one `InventoryRequest` covering every board this node
carries, and the push to that seed (§12) then runs on the answer — issue
#478 reversed the original order, because the response is now what tells
the push what to send. The peer-list request comes last of the three: all
three draw on one per-source request budget at the seed (§13.9), and
candidate discovery is a resilience path for a later pass, while
hello/inventory/push are what the pass exists for. Not sent to one arbitrary "best" peer — every seed
already dialed that pass gets asked, since not every peer necessarily
carries every board this node does, and the push runs against all of them
regardless. A seed that carries none of the requested boards simply
returns an empty event list; this is indistinguishable from (and no more
expensive than) today's existing per-seed push tolerance for an
uncooperative peer.

**Push direction: send what the peer asked for, not everything (issue
#478).** An `InventoryRequest` is already exhaustive — every board,
channel and file area the requester carries, each mapped to the full set
of content IDs it holds. That body therefore already states everything
the *responder* could want from the requester, so the responder answers
with both halves of one comparison: `events` (what the requester lacks,
above) and `wanted` (the declared content IDs the responder itself lacks).
The requester then pushes exactly the `wanted` events it originated. No
second round trip, no new request field, and no per-peer push cursor to
persist.

**Scope: a resource the requester declared, and nothing else.** `wanted`
answers "do you hold this?" for every content ID the request lists, so what
it may consult is a disclosure boundary, not an implementation detail. It is
computed per declared resource — the materialized sources union whatever
`link_events` holds under that same resource id — never against this node's
global dedup set. That set spans `link_message`s, their acknowledgements and
`key_transition`s, all of which this section deliberately excludes from
inventory; consulting it would let any completed peer file a known content ID
under a fabricated resource and read an exact membership answer off the
response. A signed request identifies the asker; it does not make an ID belong
to the resource it was filed under.

A resource this node has **seen and not taken on** — offered or excluded
(§9.3), which is what a carry-quota refusal or a SysOp's exclusion leaves
behind — wants nothing further. Asking on would be asking for content this node
has not taken on, and would not terminate: such a board's posts never reach
`link_events` at all, so they would be wanted every pass forever and occupy the
requester's whole push page. A resource never seen is the opposite case and
still wants everything declared for it, which is how a genesis arrives by push.

The same resource needs the mirror rule on the other side of the exchange
(issue #669). It is absent from the requester's maps, and absent means "never
seen" (issue #94), so every responder carrying it would send its genesis and
every event under it on every pass, under the one `_MAX_EVENTS_PER_REQUEST`
budget all three kinds share: a declined board with a couple of hundred posts,
sorting early, would be the only thing the requester ever received from anyone
carrying it. So the request carries a signed `not_carried` list, by kind, of
the resources the requester holds a genesis for and has no local copy of, and
the responder leaves them out. Deleting a Linked resource keeps its genesis on
file for this, including one this node originated, whose genesis otherwise
lives only in its own row. The list is capped at 5,000 IDs per request: the stored
genesis set is not bounded by anything the node controls, since a peer can keep
sending geneses to a node past its cap, and an unbounded list would grow until
every request was refused. Over the cap each request declares a fresh random
sample, so what goes undeclared costs a resend, never a fixed starvation.
Against a responder that takes pages (issue #685, above), the requester instead
uses enough pages that each page's share stays well under the cap, so each
request declares every declined resource on its page, the responder skips every
undeclared resource off it, and none is ever resent; the sample remains only as
the backstop. The existing maps cannot say this, since their
values are known-ID sets and an offered board's posts were never received. The
field is part of the signed payload only when it names something, so a request
without it signs exactly as before; and it is sent only to a responder whose
descriptor advertises `inventory_not_carried` (§8.2), because an older
responder would rebuild the payload without it and refuse the whole request.
Against an older responder the old behaviour remains. Until issue #683 records
carry states, the list is inferred from `link_events` the same way as "seen and
not taken on" above.

**The cap goes on the push, not on `wanted`.** `wanted` is returned whole:
it can never exceed the content IDs the requester itself just declared, which
the responder's `client_max_size` already bounds, and prefix-capping a list the
*responder* orders is the one thing that must not happen here. A requester
carrying more events originated elsewhere than the cap allows would see those
unsendable IDs fill the page, drop every one of them at the filter below, leave
the responder's state unchanged, and get the identical page back next pass —
its own events never offered at all. The requester caps instead, after
filtering `wanted` down to what it originated: that truncation only ever drops
events it can actually send, so the responder has them next pass and the list
strictly shrinks. No cursor is needed for the remainder, for the same reason the
pull direction needs none.

Two things stay outside this. `key_transition`s are pushed
unconditionally every pass, because identity events are outside inventory
scope (the Scope paragraph above) and a peer has no way to ask for one;
they are few and dedup makes a re-send a no-op. They ride in the same request
while there is room, so an ordinary node's whole push is one request — but
resource events keep at least half a request's capacity whatever the rotation
history looks like, and a long history simply costs a second request rather
than starving them. And the push still only ever carries *self-originated*
content — a `wanted` entry the requester merely carries is skipped, preserving
the "no relay from a stranger" scope note; the responder reaches that content
through its own inventory pull, which is what the multi-hop diff exists for.

`wanted` is required, not optional: a 200 response without it is malformed
and refused. Every node on this mesh runs the same release, so there is no
older peer to accommodate, and accepting a missing key would quietly turn a
broken responder into a degraded-but-working exchange.

That leaves exactly one way to finish a pass without a `wanted` list — the
inventory exchange itself failing, e.g. a peer whose `/inventory` route errors
while `/events` still accepts a push. Such a peer still gets pushed to, from a
rotating starting point held in memory for the lifetime of one sync loop, so
successive passes walk the originated history instead of re-offering its head.
Without that, a node with more than one page of own events would never deliver
the rest to a peer whose inventory route stayed broken — and in an asymmetric
topology, where that peer never dials back, its own pull cannot make up the
difference. The offset is deliberately not persisted: a restart simply begins
the walk again, which dedup makes free.

**What this replaced, and why it was a real defect.** The push previously
sent every locally originated event to every seed every pass, sliced into
requests of `_MAX_EVENTS_PER_REQUEST`. Past roughly 3,800 originated
events — reachable once §11.2 began announcing every upload — that
exceeded the peer's entire per-source request budget (§13.9,
`request_rate_capacity`): a pass spent the whole budget on the same early
slices, took an HTTP 429, and began again at the first slice next pass.
The tail was never reached at any point. Pull-based catch-up still
converged the peer, so this was a starved optimization rather than lost
content, but a push that provably cannot deliver its own tail is not a
push.

**No loop or amplification guard is needed beyond what already exists.**
This is pull-based and diff-first by construction: nothing is transmitted
unless a requester explicitly asks for a board it has already decided to
carry, and the diff is always relative to what the requester already
reports having. A fully-connected mesh does not flood — it converges,
because every node's own request naturally shrinks once it has caught up,
and dedup (`known_event_ids`) makes any redundant delivery a no-op
regardless.

**Schema change.** `link_events` gains a nullable `board_id` column,
populated for the five object types above (read directly from each one's
own `payload["board_id"]`) and left `NULL` for every other object type.
Backfilled for existing rows via `json_extract` against the stored
envelope, never requiring re-verification of already-accepted events. A
covering index on `(board_id, object_type)` keeps the diff query cheap as
`link_events` grows — this is exactly the kind of query the table did not
need to serve before this issue, since nothing previously asked "everything
for board X" rather than "everything from sender Y."

**Deliberately not addressed here** — sending a requester's complete
per-board content-ID list every pass does not scale indefinitely for a
board with a very large post history; a compact digest (Merkle-tree-style
or otherwise) would reduce request size for that case. Not worth building
at this project's declared scale (§2.3: dozens-to-low-hundreds of
concurrent sessions, small-to-medium Link deployments) — the same
"exact IDs, not Bloom filters" simplicity §8.7 already chooses for local
dedup storage applies here to the wire exchange too. Revisit only if a real
deployment shows this cost is actually a problem, not preemptively.

**Explicitly deferred to issue #86, not part of this issue.** No retention
or purging of `link_events` changes as part of this work — every event
handled here is durable, unbounded-lifetime state exactly as it already is
today. Issue #86's retention/purge policy must be designed with this
issue's shape in mind (a purged event a slow-to-reconnect peer still needs
for catch-up must never be silently unavailable, or "eventually converges"
above would stop being true) — that is precisely why #86 is sequenced
after this issue, not the other way around.

### 8.9 Event/dedup retention (issue #86)

**Part 1: the chain-idempotency gap `netbbs.link.store` named.** Every
board-scoped `handle_events` branch self-heals an exact resend against its
own *authoritative* state (never against `known_event_ids` alone) — except
`board_origin_transfer_offer`/`_accepted`, which previously depended
entirely on the fast dedup cache still holding the content_id. A resend of
a still-pending offer, or an already-accepted transfer, after a
hypothetical cache purge would have been misread as a genuine conflict
("already has an outstanding offer" / "no outstanding offer on file") and
rejected — not a security hole (nothing is ever mis-applied), but exactly
the gap blocking any purge policy from being provably safe. Fixed the same
way `key_transition`/`board_post_edit` already do it: check whether the
incoming event's own `content_id` already matches the current pending
offer (`board_lifecycle.pending_offer`) or the current lifecycle head
(`board_lifecycle_head`) *before* treating a second sighting as a
conflict, self-healing `known_event_ids` from that authoritative state
rather than erroring.

**Part 2: what can actually be purged.** Before choosing a retention
window, each object type was traced for what *else* depends on its
`link_events` row surviving — restart reconstruction (`load_link_node`)
and, since issue #85, this node's own ability to answer an inventory
request for it:

| Object type | Durable elsewhere? | Purgeable in this issue? |
|---|---|---|
| `key_transition` | Yes — `link_peers.transitions_json` is the authoritative source `load_link_node` reconstructs `sender.transitions` from; the `link_events` row exists only to fast-path a resend and was never itself load-bearing. | **Yes.** |
| `board_genesis` | Yes — `boards.link_genesis_json` durably holds it for both self-originated and carried boards, read unconditionally by both `load_link_node` and §8.8's own `_all_board_events`. | Not in this cut (see below). |
| `board_post` | **No**, for a peer-received post — `posts.link_event_json` is only ever populated for a *locally-authored* post (`queue_board_post_if_linked`), never for a materialized/carried one. Its `link_events` row is the *only* record `board_post_edit`'s own `self.events.get(root_post_id)` acceptance check and §8.8's inventory diff can draw on. | No. |
| `board_post_edit` | Same gap as `board_post` for a peer-received edit — `post_edits[root_post_id]` reconstruction and inventory serving both depend on the row. | No. |
| `board_origin_transfer_offer`/`_accepted` | **No** — `board_lifecycle_head`/`pending_origin_transfers`/`board_origin` are reconstructed *entirely* from `link_events` rows for a peer-received transfer; nothing else durably records "what the current lifecycle head is." | No. |
| `link_message` family | Not traced in this issue — deferred with the rest of this row. | No. |

**Policy actually implemented: a bounded, age-based purge for
`key_transition` only.** `netbbs.link.store.purge_expired_key_transitions`
deletes `link_events` rows where `object_type = 'key_transition'` and
`received_at` is older than a fixed retention window (90 days — a plain
module constant, not a new SysOp-configurable `LinkConfig` field, matching
this project's own restraint principle: a low-volume event type doesn't
need a dedicated tunable yet). Called inline on every accepted
`key_transition`, the same "purge on write, scoped to the same table this
write just touched" shape `LinkDiagnosticLogHandler.emit` already
established for `link_diagnostic_log` — not a separate scheduled task.

**Everything else stays unbounded in this issue, explicitly, not
silently.** `board_genesis` turned out to already be redundant with
`boards.link_genesis_json` and could plausibly be purged too, but is left
alone here to keep the rule simple (nothing board-scoped is purged this
round) rather than special-casing one board-family type while the other
four remain load-bearing. Purging `board_post`/`board_post_edit` safely
would need a real answer to "has every peer that might still need this via
inventory already caught up" — a harder question than this issue's own
scope, and a legitimate follow-up if `link_events` growth from board
content specifically ever becomes an operational problem in practice
(§13.6's `[L]ink status` already gives a SysOp visibility into growth via
the database file size, per the existing diagnostic-log-growth precedent).

### 8.10 Real-time Noise sessions (issue #148)

Real-time Link traffic uses a persistent TCP connection protected by
`Noise_XX_25519_ChaChaPoly_BLAKE2s`. XX is required because either side may
first encounter the other's current operational key during the connection;
neither side assumes the remote static key is preconfigured. This is a
separate traffic family from signed HTTP+JSON. A live chat frame is not a
canonical Link event and never enters inventory, relay mailboxes, event
retention, or asynchronous retry queues.

The Noise static X25519 key is derived from the existing Ed25519 operational
transport key using PyNaCl/libsodium's supported Ed25519-to-Curve25519
conversion. NetBBS does not create a fourth long-lived node key or introduce a
second transport-key rotation mechanism. During the encrypted XX handshake
each side sends a versioned identity payload containing its stable root
fingerprint, root public key, and root-signed transport transition chain. The
receiver:

1. verifies the root fingerprint and transition chain;
2. resolves the current authorized Ed25519 transport key;
3. converts that public key to X25519;
4. requires it to equal the Noise static key authenticated by the handshake;
5. applies the local Phase-4 node transport decision before accepting any
   application frame.

A stale, revoked, forked, malformed, or differently bound key fails the
connection. The remote node label, endpoint, DNS name, and TCP address are
never identity authority. Transport-key rotation ends sessions using the old
key; reconnect performs a fresh handshake against the new verified chain.

The endpoint descriptor advertises real-time TCP addresses separately from
HTTP addresses. An outgoing-only node may dial a reachable full node. Two
nodes which both cannot accept inbound connections meet through a live relay
(§8.10.3, issue #168) -- a raw-socket proxy below the Noise layer, a separate
mechanism from the asynchronous relay mailbox, never tunneled through it.
Asynchronous linked-channel events continue to work regardless.

A node whose only way out is an HTTP proxy opens every real-time socket as a
`CONNECT` tunnel through the proxy asynchronous Link uses (`HTTP_PROXY`, else
`HTTPS_PROXY`, subject to `NO_PROXY`) and runs the same bytes over it: the
attach preamble, the Noise handshake and the session are unchanged, and of the
Link traffic the proxy sees no more than an on-path observer of a direct
connection would (Basic proxy credentials, where used, are the proxy's own and
cross to it in the clear). The target authority is validated strictly before it
is sent. The tunnel is the only attempt when a proxy applies, an unsupported
proxy URL fails the dial rather than dialling direct, authentication is Basic
from the proxy URL or `~/.netrc`, and a refused tunnel or a failed handshake
over an open one is recorded for the Link status screen. A proxy that inspects
TLS inside the tunnel cannot carry Noise. Decisions and rationale: §16, issue
#628.

#### 8.10.1 Session framing and ownership

Handshake and transport records use an unsigned two-byte big-endian length
prefix followed by exactly one Noise message. Zero-length records are invalid.
The Noise limit of 65,535 bytes is an absolute ciphertext ceiling; NetBBS sets
a lower application plaintext limit of 16 KiB. Decrypted application payloads
are strict UTF-8 JSON objects: duplicate keys, floats, unsafe integers,
unknown protocol versions, missing required fields, and trailing data are
rejected. Unknown message types produce a bounded protocol error and do not
gain side effects.

Every application object contains `version`, `type`, and a session-local
`message_id`. IDs are bounded strings and deduplicated within a bounded
per-session replay window. The first implementation supports:

- `subscribe` and `unsubscribe`;
- `presence_snapshot` and `presence_delta` (channel-scoped);
- `node_presence_snapshot` and `node_presence_delta` (issue #164, node-wide
  -- see §8.10.2);
- `channel_message`;
- `scrollback_snapshot` (issue #194);
- `relay_request`, `relay_waiting`, `relay_ready`, `relay_reject` (issue
  #168, §8.10.3 -- the live-relay rendezvous);
- `direct_message` (issue #168, §8.10.3);
- `ping`, `pong`, `error`, and `close`.

One node owns at most one live session per remote fingerprint. If simultaneous
inbound and outbound connections exist, the lower fingerprint keeps its
outbound connection and the higher fingerprint keeps its inbound connection;
the rule is applied only while both candidates exist, so a sole usable
connection is never discarded. Reader, writer, heartbeat, and reconnect tasks
are owned by one session/supervisor object. Its close path cancels and gathers
every task without masking the initiating failure.

Outbound frames use a bounded queue and never let one slow peer block another.
Subscriptions, remote presence entries, message rate, protocol strikes, and
concurrent handshakes are bounded per peer and node. A full queue drops the
session with an explicit slow-consumer reason rather than silently losing a
state transition. Heartbeat leases expire silent peers; reconnect uses bounded
exponential backoff with jitter and resets only after a stable authenticated
session.

#### 8.10.2 First live linked-channel vertical

A subscription names an already carried `channel_id`. The receiving node
checks that the channel exists, is linked, is locally allowed by trust policy,
and is available to the subscribing peer. Authorization is checked again for
every received message; a successful subscription is not a permanent grant.

Live channel messages are ephemeral node-attested assertions. They carry the
canonical local user ID and display label at the authenticated sending node,
the channel ID, body, creation time, and session message ID. They are not
individually signed canonical events: the authenticated Noise session
attributes them to the sending node, and the UI renders the human identity as
`user@node`. Password-only users can therefore participate without acquiring
a personal signing key. The sending node remains responsible for enforcing its
local membership, mute, and moderation rules before transmission; the
receiving node independently enforces its own node/user/content policy before
display. The receiving node retains the authenticated sending-node fingerprint
on the in-memory message even though ordinary rendering shows the friendly
label. If that fingerprint has an undismissed cryptographic-identity collision,
the live channel line carries the same non-blocking caution used at mail and
direct-message interaction boundaries.

Live lines flow both ways over the one session a subscriber holds to the
channel's origin (issue #860). A subscriber sends its callers' lines up that
session, and the origin shows them to its own callers and relays them to its
other live subscribers, so a Linked channel is one room live. The relayed frame
names the author's node in an optional `author_node_fingerprint`. A receiver accepts
`author_node_fingerprint` only from the channel's origin; anyone else naming
another node is a protocol strike. A relayed line meets the same policy as its
signed event: the author's node must be one allowed to publish here (`EVENTS`),
and the caller must not be quarantined or blocked. A node that has not
established the author's node sees the line neither live nor by sync. Older
nodes refuse unknown frame keys, so that key goes only to peers advertising the
`channel_relay` capability. A frame's optional `content_id` is accepted and
ignored: it is the sender's unverified claim. An older subscriber gets relayed
lines through async catch-up instead; an older origin still shows a
subscriber's plain frame to its own callers.

A line that arrives by async sync is shown at once to callers already in its
channel, not only on their next join. The bridge remembers the lines it has
shown (bounded), so the signed event that follows a live line is not shown
twice. A line is keyed on what it is -- author's home node, author's local user
id, `created_at` and body -- derived locally the same way from the live frame
(author node: the session peer, or the node the origin names) and from the
signed event's verified payload; the sender builds both from one stored row. A
peer therefore can only mark its own callers' lines as shown, never suppress
another node's line by claiming its id.

Presence is leased, scoped to subscribed linked channels, and advisory. A
snapshot establishes current state after subscription; deltas update it.
Disconnect or lease expiry removes that node's remote presence without
persisting synthetic leave events.

**Node-wide presence (issue #164)** is a second, independent presence
concept, not a generalization of the channel-scoped one above: it answers
"who's online on this node, right now" -- the same question the local Who's
Online screen already answers -- across every node a live session currently
exists with, not gated on shared channel membership. A node broadcasts a
`node_presence_snapshot` (the local online roster) the moment it starts
tracking a peer's session, then `node_presence_delta` on each subsequent
local login/logout (the account's *first* concurrent session and *last*
remaining one only -- multi-session accounts don't flap). No new trust check
was needed: establishing a live session at all already requires `ESTABLISHED`
transport trust (§12), so node-wide presence inherits that gate for free.
Caller-facing: `[W]ho's online` mixes in every currently-known remote entry
alongside local sessions: since Link-wide live private chat doesn't exist yet
(§8.10 above), selecting a remote entry states that plainly rather than
silently failing or offering an action that doesn't work.

A freshly-subscribing peer also receives a bounded, ephemeral
`scrollback_snapshot` of the origin's own recent local scrollback,
rendered once and never durably stored on the subscribing side (§16,
issue #194) — a shrunk window before the existing async catch-up path
below fills in what a live-only subscribe would otherwise miss, not a
new durability promise. The first vertical still does not offer multiple
background channel subscriptions per caller (issue #159, closed — decided
against, not a gap); live private messages and relayed sessions arrived
with §8.10.3 (issue #168). A disconnect does not queue or replay live
frames. Callers see `connecting`, `live`, and `offline/degraded` state
plus an honest notice that live traffic may have been missed;
asynchronous signed linked-channel events remain the durable catch-up
mechanism until a later decision changes that product model.

That asynchronous catch-up path (issue #164) now enforces the identical
author-trust-state visibility linked board posts already do: a linked
channel's scrollback silently omits a message whose signed author's home
node is currently `BLOCKED`/`QUARANTINED`, keyed on the event the message
carries (`link_content_visible`), never on which node happened to relay it.
`PROBATIONARY` and local messages are unaffected -- boards and channels now
share one visibility policy instead of two independently-decided ones.

#### 8.10.3 Relayed sessions and live direct messages (issue #168)

**Live relay.** A relay is any full peer with relay serving enabled
(`[link] relay_serving_enabled`, the same switch as the asynchronous
mailbox) that both parties currently hold an ordinary authenticated
real-time session with; the reliable-nodes roster (§8.3, §16 issue #219
Decision 4) is how an outgoing-only node knows which relays to stand by
at -- it keeps a reconnecting session to every reliable node it knows
while participation is accepted, since by definition nobody can dial it
first. The relay is a raw-socket proxy below Noise (§16 issue #168
Decision 1): it never holds key material, sees only ciphertext, and the
two parties run the unchanged Noise XX mutual handshake with *each other*
through it.

Rendezvous rides over the parties' existing sessions to the relay:
`relay_request {target_fingerprint, requester_fingerprint}` from the
requester; `relay_waiting` back while the target is asked; the same
`relay_request` shape forwarded to the target as an invitation (its own
fingerprint as target); the target's agreement (a `relay_request` naming
the requester) or `relay_reject {reason: declined}`; then `relay_ready
{bridge_id, peer_fingerprint, role, attach_token, attach_address,
attach_port}` to both. Each party opens a fresh TCP connection to the
attach address, sends one plaintext `NETBBS-BRIDGE/1 <token>` record, and
runs Noise XX in its assigned role (requester initiates). Both roles
verify the authenticated fingerprint against the one `relay_ready` named
before admitting the session -- the relay is an intermediary and could
pair anyone with anyone; this check is what stops that. The relay makes
no trust decision about the pair; each party applies its own `REALTIME`
policy to the other before agreeing and again after the handshake, as
for any direct session. Relaying carries no Phase-4 implication.

Bounds (Decision 2), all operator-adjustable under `[link]`, each breach
an explicit reject/close: `live_relay_max_concurrent_pairs` (8),
`live_relay_max_pending_rendezvous` (32) with
`live_relay_rendezvous_timeout_seconds` (30, reported back as
`relay_reject {timeout}`), `live_relay_max_bytes_per_second` per
direction per bridge (64 KiB -- a byte-rate bound, since the relay never
parses frames), and `live_relay_idle_timeout_seconds` (120, a dumb
"no bytes either way" timer the endpoints' own ping/pong keeps from
firing). One leg closing tears down the other. Reject reasons are a
closed set: `not_serving`, `invalid_target`, `target_unreachable`,
`at_capacity`, `pending_full`, `declined`, `timeout`, `attach_failed`,
`policy_refused` (the requester's own standing session no longer passes
the relay's `REALTIME` policy; a *target* that no longer passes is
reported to the requester only as `target_unreachable`). Every relay
answer echoes the requester's `relay_request` message id as `request_id`;
an invitation's message id is echoed by the target's agreement or
decline; a forwarding relay's upstream request id is echoed on the
upstream's answers -- so no answer to an earlier, expired attempt can
ever settle a fresh one for the same pair.

These frames made the real-time application protocol **version 3**; requiring
the invitation id on party agreements and declines makes it **version 4**.
A version-3 relay rejects that new field, just as a version-2 peer cannot
accept the version-3 frame types, so mixed versions fail once at the
authenticated handshake with the caller-visible upgrade notice rather than
timing out a rendezvous or dropping a shared channel/relay-anchor session.
A `relay_reject` carries `origin` (`relay` or `party`) so a node
that is both a relay and a party can never misroute one. Every relay
frame a party honours is correlated: a `relay_ready` or `relay_reject`
counts only from the relay this node asked, for the target it asked
about, or from the relay whose invitation it accepted, within the
rendezvous timeout -- an authenticated peer can never make a node open an
outbound connection to an address of its choosing.

**Live direct messages.** `direct_message {to_user_id, from_user_id,
from_display_label, body, created_at}` is one private line between a
user on the sending node and a user on the receiving node, over whichever
session exists or can be established: the registry's existing session,
a direct dial of the peer's advertised real-time address, or a relayed
session -- in that order. It is ephemeral and node-attested like a
channel message (§8.10.2): never stored, never a canonical event. The
receiving node re-checks the sending node's `REALTIME` policy at delivery,
then delivers exactly as a local `/msg` does (live chat sessions via the
hub, every other session via the mailbox); an unknown, opted-out, or
offline recipient, or one who blocks the sender's `user@<fingerprint>`
(§6.4 Blocked people, issue #925), is dropped silently -- the sender already checked the
peer's node-wide presence (a node that has not yet pushed its presence
gets "couldn't confirm who is online there", never a blind send), which
the peer pushes the moment the session is tracked -- and receiving a
node-presence snapshot is itself what makes the receiving side track a
session that never subscribes to a channel, so the presence is answered
and later cleared. A remote user list is not a caller's to probe.

**Anchor advertisement and chained bridges (issue #270).** A node's
signed endpoint descriptor carries an optional `live_relays` list -- the
reliable nodes it is currently standing by at (§8.3; omitted when empty,
like `relays`) -- refreshed with every hello. Session establishment then
tries, in order: the registry; a direct dial; a single-hop rendezvous at
each relay the *target* advertises (reusing a session or dialing that
relay directly, since a relay is a full peer); a single-hop rendezvous at
each of the node's own anchors; and, only for a target relay the node
could not reach itself, a chained rendezvous: `relay_request {target,
requester, via_relay}` to its own anchor R1, which reuses or dials R2 and
forwards the request with `hops: 1`. R2 runs the ordinary rendezvous with
the target, treating R1's session as the requester's side, and its
`relay_ready` to R1 names `for_fingerprint` (whom the leg is for). R1
attaches to R2 as a raw leg -- no handshake; it is a pipe -- issues the
requester its own `relay_ready`, and splices the two legs: A–R1–R2–B,
Noise still end to end, both relays seeing ciphertext only. A forwarded
request is never forwarded again (`hops` is capped at one), so a chain
is at most two relays; each relay counts its bridge against its own pair
cap and applies its own byte-rate and idle bounds, and every failure
along the chain surfaces to the requester as an explicit `relay_reject`.

Caller-facing (Decision 3): `/msg user@node-fingerprint <text>` and
`/private user@node-fingerprint` in chat, and `[M]essage` on a remote
entry in Who's online. When no path exists, whatever the reason, the
caller sees one reason-free refusal -- "can't be reached for live chat
right now" -- pointing at Link mail; nothing fails silently. Cross-node
`/dm` invites stay local-only in this vertical.

---

### 8.11 Learning a third node's identity from a carrier (issue #630)

A hello bundle authenticates itself (§8.2, §10.6): only the holder of a root
key can produce a transition chain that verifies against it and a descriptor
signed by the key that chain currently authorizes. Who delivers the bundle
therefore does not enter into its verification, and a carrier can serve a
third node's bundle without being trusted for anything. It can withhold one;
it cannot forge one.

**The exchange.** A node that meets, in a carrier's inventory response, an
author or origin it cannot verify asks that carrier for the identities
concerned: `POST /link/v1/identities/{requester}` with a signed
`identity_request` naming up to 32 fingerprints. The request is authenticated
as every other pull is (a completed hello with the requester, the signed
requester matching the wire peer, this node named as responder, a signature
under the requester's current key, a five-minute freshness window, a bounded
nonce cache) and is gated by the same policy action as the inventory it
accompanies. The response carries the bundles the carrier holds and omits the
rest. A carrier answers for nodes behind content it has recently served (a
bounded, in-memory set of 4,096): the authors and origins its events name,
and the current origin of each event's resource, which is who signs a
closure, a tombstone, a moderator's edit or a file descriptor without being
named in it. It also answers for a node it relays for (§8.5), whose trust
objects it may be carrying (§12.7) and which names this relay in its own
descriptor anyway. It answers for nobody else. Answering for any fingerprint a peer names would let a peer on
probation, which is refused the peer list (§8.3), read the carrier's peer set
one guess at a time, addresses included. A page of events can name more nodes
than one request may, so a requester sends up to four requests at a time,
once before policy is consulted and once more for signers that only handling
the events revealed: a bundle gone stale, or an origin its event does not
name. An identity a carrier answered without, or could not be asked for
because it lacks the route, is not asked of it again for an hour; a request
that merely failed is repeated on the next occasion. A carrier serves the identities of its peers and of nodes it was itself
introduced to: the peer list shares only first-hand knowledge because a
secondhand address is a weaker claim the further it travels, but a bundle is
not a claim, and refusing to pass one on would break a board carried across
two hops. The requester accepts only bundles it asked for, and verifies each
exactly as it verifies a hello received directly.

**What an introduced identity is.** It can be used to verify what it signed
and a third party delivered: carried board, channel and file-area events, and
trust objects fetched from a carrier (§12.7). It is not a peer. Membership of the peer set is what every route checks
to decide who may push events, pull, serve as a relay or be sent Link mail,
and an introduction grants none of that; all of it still requires a completed
hello. Introduced identities are stored apart from peers for that reason. A
completed hello supersedes an introduction, and an introduction never
replaces a peer's record. At most 1,024 are kept, in memory and on disk; at
the cap the oldest goes, since a displaced identity is simply asked for again
when next needed. It takes with it what its introduction alone created: its
name observations, and its trust subject unless a SysOp has decided something
about it or evidence is held on it. A file area whose origin is known only by
introduction can be listed and not fetched from, since chunk transfer is
never relayed (§11.3), and the file screen says so.

**Trust.** An introduced node is registered as a trust subject and starts on
probation like any other (§12.4). Under the policy a running node enforces,
its content is therefore verified and still withheld until the SysOp
establishes it, by setting both its identity integrity and its resource
behavior. That is the point of introducing it *before* policy is consulted:
a node that is not a subject reads as probationary too, and its content is
refused with no way for the SysOp to see the node, let alone establish it.
It never graduates on its own: the thirty-day age requirement counts from the
introduction, but graduation also needs days of direct activity, which a node
never met cannot have. Establishing the node does not establish its callers.
A remote user is a subject of its own (§12.4) and starts on probation, so its
first posts arrive pending approval in the board's queue like any other
probationary remote user's. The familiar-name warning of §4.4 applies to an
introduced node, and matters more than for a peer: its name is what callers
read beside every post. The comparison is one-sided. An introduced node is
compared with every node on file; a node this one has met is compared only
with others it has met, and meeting a node re-judges any introduced node that
wears its name. Otherwise anyone could have a real peer flagged as an
impostor across the network by naming a node after it and posting once on a
shared board. Mail addressing resolves names among met nodes only, for the
same reason.

**Chain length.** A hello or introduction bundle carries at most 256 key
transitions (issue #1039). A chain is walked, one signature check per
transition, on every hello, introduction and compromise sweep, so its length
must not be the sender's choice. One past the cap is refused when the bundle is
parsed, before anything verifies it. Each rotation adds two transitions, so the
cap is over a hundred rotations.

**Stale bundles.** A third node's key transitions are not gossiped as events;
a `key_transition` event is accepted only from its own subject. They do travel
as part of a chain, though. A carrier's inventory response names, under
`key_chains`, the signing-key history of each node whose content it serves in
that response, when that history holds more than the first authorization and
is no longer than 64 transitions; at most 32 chains per response, never the
carrier's own or the requester's (issue #914). It also names the chain of
whoever signed what the requester declares it holds, when that signer has
marked a key compromised: a requester holding a stale copy is served nothing of
that signer's, and would otherwise never hear (issue #672). The requester merges each into
an identity it knows **by introduction only**, checked against the root key
already on file. A direct peer's chain comes only from that peer; an identity
the requester has not been introduced to is learned whole, by introduction.
Without this, a compromise never reached an introduced-only node: its bundle
still held the compromised key as current, old-key copies verified and were
accepted, and nothing failed to prompt a refresh. With it the same pull skips
them, per object.

**Stale copies.** A copy a node already holds, signed only by a key its
signer has since marked compromised, is not re-checked by being kept; it was
accepted when that key was current. When the node learns the compromise --
from the signer's own revoke or hello, or from a carried chain -- it looks
through what it stores of the inventory-carried kinds, among the copies that
name that signer as author or origin, for those that verify under that key and
under none the signer still stands behind, once per newly
compromised key, and marks them stale (`link_events.stale_signer`). A stale
copy is neither declared in inventory nor served, so the next exchange asks
for it again. The signer re-signed its content in its compromise response
(§4.5); a content id covers the envelope and not the signature, so the
re-signed copy has the same id and the same envelope. It replaces the stale
copy in place -- the stored envelope, and a carried genesis on its board,
channel or file area row -- and the projection built from that envelope is
left as it is: the post, line or file stays visible throughout and is never
doubled. Each hop learns the compromise in turn, so re-signed copies travel
through any chain of carriers without a change on the wire (issue #672). A
second stale copy offered meanwhile, by a carrier that has not heard yet, is
dropped.

A bundle on file can still be stale another way: after a rotation it no
longer verifies what the new key signs. The requester knows which identity the
failed check was made against, whether or not the event names it, and asks the
carrier for a fresher bundle. An event that builds on one set aside is set
aside with it, whatever its own check reports: the second edit in a chain fails
as not extending the current head, which is otherwise a refusal. A fresher
bundle, like a carried chain, is **merged** into the one on file: transitions
are append-only, so a bundle from before a compromise, which verifies against
itself alone, cannot make the compromised key current again. Its descriptor
replaces the one on file only if it is not older and is signed by the current
key of the merged chain.

**Events that cannot be used yet.** The inventory is a diff (§8.8): a node
declares what it holds and is sent the rest, one page of 200 events per pass.
An event it cannot accept would be sent again on every pass, and 200 of them
would be all it ever received. A carrier's response is therefore handled one
event at a time. One whose signer is unknown, or that builds on something not
yet received, or that policy refuses, is set aside and declared in later
requests as seen, so it is neither downloaded again nor allowed to crowd the
page. It is declared under its resource whether or not this node
carries that resource: a board whose origin is on probation here is not
carried *because* its genesis was set aside. It is asked for again when the
identity it waited for becomes known, or after an hour, or when trust changes
(issue #700):

- the SysOp overrides or clears an override for that node, which also starts a
  sync pass at once;
- a trust anchor, domain, reporter or sole-authority exception is changed in the
  trust console, which releases everything set aside and starts a pass;
- the node, or a user of it, graduates or recovers on its own, found by the
  re-evaluation every pass runs (§12);
- any of these is done where the running node cannot be told, with
  `python -m netbbs.admin`. The sync loop compares a generation read from the
  trust tables those writes already leave rows in (the config and policy audits,
  the overrides), once a pass, and releases everything set aside when it moves.
  No marker of its own is written.

**Every hour-long wait is shown and can be ended (issue #700).** Besides events
set aside here, a node also waits an hour before offering again an own event a
peer refused one by one (#897), asking a carrier again about an identity it
could not introduce (and refreshing a reporter known only by introduction),
and depositing trust objects again at a relay that refused them (#627). Each
hour keeps an unfixable refusal from costing a download or a request every pass;
none of them told a SysOp anything, so an operator who had just fixed the cause
could not see whether they had. Link status lists them under **Waiting**, with
when each is next tried, and **[R]etry now** ends them all and wakes the sync
loop for a pass at once: `LinkNode.sync_wake`, an event the loop's sleep waits
on beside its stop event. The deposit backoff is in the database, so retrying
moves the refusal's time back past the backoff and keeps its text for the vouch
screen. A newer descriptor for a node known only by introduction, arriving in a
peer list and verifying against its signing key on file, replaces the
introduced copy as a peer's would (issue #270), so a reporter that has just gained
a relay is reachable without waiting for the hourly refresh. At most 10,000
events are set aside, a third of what an inventory request can declare. Past
that the oldest are offered again and can crowd a page, and the remedy is the
SysOp's: establish or block the nodes concerned. Declaring it is not a claim to hold it, and costs nothing if read as
one: a responder that lacks the event would ask for it back, and a node only
ever pushes what it originated. An event that is *wrong*, from a peer, still
ends the response there; what was accepted before it is kept and persisted.

### 8.12 The node map (issue #767)

The node map lists the other NetBBS boards this node knows, how it knows each
one, when it last heard of it, and how a caller dials it. It is the network's
BBS list, as one board sees it. Callers and the SysOp see the same screen; the
SysOp's shows more.

**What is listed.** Every node this one has completed a hello with, every
node a carrier has introduced (§8.11), and every node that is the origin of a
board, file area or linked channel this node carries, even after its introduced identity was
displaced from the bounded store; such a node is listed under whatever name
this node still has for it, or the unknown-node label, and every field no longer
on file (provenance, dial-in, last heard, addresses) reads *unknown*. A node is
listed once, from its best-verified source: met before introduced, introduced
before candidate. For callers the list leaves out every node
that is quarantined or blocked in any of the identity, resource or content
dimensions (§12.2): this node refuses or withholds something of theirs,
so leaving them off tells a caller the truth about what reaches them here.
Operational reachability is not a trust state and hides nothing. Probation is not a reason to leave a node off; nearly every new
node is on probation. A node cannot ask to be left off. The list's worth is
that a missing board is one that callers cannot reach through this board,
because this board either does not know it or refuses its content. A list
that other SysOps could punch holes in would lose that meaning. The
list is not the whole network: peers pass on only the nodes they have met
themselves (§8.3), and there is no directory. The screen says so in its title,
"Nodes known to <board>", and nowhere else.

**What a caller sees.** Per node: its friendly name, with its DNS name beside
it only where two listed nodes share a friendly name (the DNS name is otherwise
in the detail view, and search matches it); how this board knows it, *direct* (met) or *via <carrier>* (introduced; *via another
node* when the carrier is itself left off the caller's list); when it was
last heard of, as a relative time, marked stale past 30 days; and, in its
detail view, the `dial_in` addresses its descriptor carries (§8.2) and the
boards and file areas this board carries from it that this caller could open
anyway, filtered by the same read gates as ordinary browsing. Link addresses, relay roles
and reliability are never shown to callers: they are how nodes reach each
other, and a caller cannot use them.

**Last heard.** The later of this node's own last direct contact with it (a
completed hello or events exchange, or an authenticated real-time session for
as long as it stays open; issue #766) and the `created_at` of its
newest valid descriptor, but never later than the time this node first
stored that descriptor, so a descriptor dated in the future cannot keep a node
fresh. A node signs a fresh
descriptor for every hello it builds, so that time is the node's own signed
statement that it was running, and a carrier who passes it on can withhold a
newer one but not forge one. A node that has never been heard from directly
still has one. Knowledge secondhand about a node's *address*, which peer lists
also refresh, is not contact and never advances it. Stale nodes are marked,
not removed.

**The SysOp's view.** The same list, plus the nodes callers do not see:
peer-list candidates, marked unverified and never shown to callers, since a
candidate has completed no hello and names nobody who vouched for it. A
candidate's descriptor is unverified, so it has no last-heard time; its row
shows *never heard from* and, labelled as such, when a peer list first
named it; and
quarantined and blocked nodes, with the state of each dimension. Each row adds the Link
addresses, relay roles and reliability. It replaces the peer list behind the
Link status screen. A node that is a trust subject here (issue #820) has trust
actions on its own screen, the same code as the subject's screen under Policy
trust: `[E]stablish` and `Bloc[k]` open the override editor with every
dimension and the state already chosen (the reason, the audited-deviation
confirmation and the audit are unchanged), `[C]lear override` can clear all of
a subject's overrides at once, and `[T]rust details` opens the subject's full
trust screen. A peer-list candidate has none: nothing about it is verified.

**Who may open it.** A node-wide minimum level set in the SysOp console,
defaulting to 0. A guest is an account (§4.6), so a SysOp who wants the map
kept from guests sets the level above the guest account's. On a node with Link
disabled the entry is not shown.

## 9. Linked boards and resource lifecycle

### 9.1 Promotion and genesis

An existing local board may be promoted into Link scope. Promotion creates one
signed `board_genesis` referencing the existing stable board ID; it does not
replace the board with another local object.

The node identity is the origin authority. The genesis includes descriptive
metadata and recommended defaults for carrying nodes.

### 9.2 Posts and edits

Only approved local posts are originated as `board_post` events. Password-only
users currently use the `node_vouched_user` author tier.

A post drawn in the ANSI art editor carries an optional `"layout": "art"` in its
`board_post` payload. The key is omitted for prose, and an absent or unknown
value is prose. Edits carry no layout: a revision follows its root (§16, issue
#711).

Self-authored edits become chained `board_post_edit` events. The original post
remains immutable. Moderator edits and tombstones require separate authorized
event types and advanced governance.

### 9.3 Carry and local materialization

A peer accepting a valid board genesis materializes a real local board copy so
users can browse carried content through the normal board UI. Carrying is more
than retaining raw protocol events — the same principle extends to a carried
board's *content*, not just the board shell itself (issue #73): an accepted
`board_post`/`board_post_edit` must become an ordinary local `posts` row, not
remain a protocol-layer record a caller-facing screen can never reach. Before
this, a carried board could verifiably receive posts while still showing
empty to every reader — `link_events` is necessary for protocol verification
and replay safety, but it is not the product database.

**Mechanism.** `netbbs.link.boards` gains `materialize_carried_post`/
`materialize_carried_post_edit`, mirroring `materialize_carried_board`'s own
shape: idempotent (keyed on the event's own `content_id`), bypassing
`netbbs.boards.posts.create_post`/`edit_post` entirely (those require a local
`User` author and mint a fresh local ID, neither of which fits received
content) in favor of a direct insert using the **event's own `content_id`
verbatim as the local `post_id`** — the same "never mint a second ID for the
same thing" precedent `materialize_carried_board` already established for
`board_id`. This has a valuable side effect: since a `board_post_edit`'s own
`root_post_id`/`previous_event_id` payload fields already name other events'
`content_id`s, and those become the corresponding local `post_id`s verbatim,
`posts.root_post_id`/`edit_of_post_id` resolve directly from the Link
payload with no separate ID-translation table.

Like genesis intake since issue #683 (which writes the genesis and its carry
outcome in one transaction), the new functions perform the `link_events` insert and
the `posts` projection in the same call, one transaction, one commit: a crash
between them is no longer possible for posts/edits specifically. `LinkServer.
_handle_events` calls the combined function once per accepted `board_post`/
`board_post_edit`, replacing today's separate `save_event` dispatch for those
two object types.

A reply's `parent_post_id` is set only if that parent is *already* locally
materialized — the same "no backfill, no speculative storage" rule this
project already applies everywhere gossip can arrive out of order (§8, §9.1):
an orphaned reply is materialized as a top-level post rather than blocked or
queued waiting for a parent that may never arrive.

**Author identity.** A materialized post's `author_user_id` is `NULL` — no
local account is implied or required by carrying content (issue #73's own
required test scenario) — with `author_label` synthesized as `local_user_id@
home_node_fingerprint` (the same address shape Link mail already uses) and
`author_fingerprint` left `NULL` (that column is a *local* user's own
personal keypair fingerprint, a different concept from a remote node's
fingerprint — never conflated). This requires a prerequisite fix: `Post.
author_user_id` is currently typed as a required `int` even though the
column has been nullable since the account-deletion migration (round 60's
`ON DELETE SET NULL`) — display code (`netbbs.net.login_flow`'s post-reading
screen, notably) currently calls `get_user_by_id(db, post.author_user_id)`
unconditionally. Widening the type to `int | None` and guarding every such
call site is corrected as part of this work, not deferred — a locally
deleted user's own old posts were already silently exercising this exact gap
before a remote author's posts could.

**Display and resolution.** No new resolution logic is needed:
`_resolve_current_version`'s existing `root_post_id`/`created_at DESC, id
DESC` query already picks the correct latest revision for a materialized
chain, since materialization always processes a verified edit chain in
accepted order — local `id` (strict insertion order) therefore agrees with
logical edit recency even when the remote-claimed `created_at` doesn't (clock
skew, out-of-order network delivery), the same tie-break reasoning issue #68
already established for purely local edits. `created_at` on a materialized
row is the *authored* timestamp from the signed event, never the local
arrival time — see the separate node-local-arrival-order issue (#72) for why
unread/New Scan ordering is a distinct concern from this display field.

**Local moderation stays event-history-safe by construction.** `delete_post`
only ever touches `posts`, never `link_events` — deleting a materialized
post's local row (subject to its own existing FK-blocker rules: no deleting a
post with local replies or edit-chain descendants) already cannot rewrite or
lose the signed record needed for replay safety, with no new mechanism
required. Origin recommendations (§9.1) never override this local policy,
exactly as they never override any other local access/moderation/retention
decision on a carried board.

Concretely, since issue #677:
- A carrying node whose copy of a board is **moderated** holds every received
  `board_post` in its own pending queue, as it holds local posts.
- A received `board_post_edit` is held there too when:
  - this node's copy is moderated;
  - the author's trust requires approval (§12.8); or
  - no revision of that post is approved here yet.
- An origin's `board_post_moderator_edit` is not held for local moderation,
  since it is the origin's own moderation. It is still held while no revision
  of the post is approved here, because a revision must never publish a post
  nobody here approved.
- Approving a held carried post only publishes it locally. It is already on
  the network under its author's signed event, and it is never re-signed as
  this node's own. Approving a held *edit* signs the edit event matching
  whoever made it: `board_post_edit` for the author, `board_post_moderator_edit`
  for a moderator on the origin. The editor is read from the moderation log,
  because `edit_post` carries the author forward onto every revision.

**Idempotency, New Scan, and search.** Duplicate delivery of an
already-materialized event is a no-op (existing `post_id` found, row returned
unchanged) — no duplicate local posts or revisions. `[N]ew scan`/unread
counts (`netbbs.activity`, issue #56) need no new wiring at all: they compare
a stored cursor against `posts` rows directly, with no separate
"mark as new" call site, so any newly materialized row is automatically new
activity. Local search does need an explicit call — `netbbs.search.
reindex_post(db, board_id, root_post_id)`, the same call every other
`posts` write path already makes, right after each materialization.

**Repairing a gap.** Because persistence and projection are now atomic for
new events, a `board_post`/`board_post_edit` in `link_events` lacks a
corresponding `posts` row only where a node carried boards *before* this
feature shipped, where the expiry sweep deleted it, or where a moderator
rejected it -- and a rejection is recorded so that it stays that way (below). A repair pass — scan `link_events` for `board_post`/
`board_post_edit` rows with no matching `posts.post_id`, and materialize them
in chain order — closes that one-time gap and doubles as the "supported
rebuild path" issue #73's own acceptance criteria ask for, the same
"derived state must be rebuildable from authoritative data" principle issue
#74 applies to FTS indexes. Exposed as `[R]epair carried posts` in the
SysOp `[S]ystem` submenu (only shown when Link is enabled), the same
explicit-SysOp-trigger-only shape `netbbs.files.gc`'s reference-aware blob
reclaim already established — purely additive (fills in a missing row from
an already-verified signed event, never deletes or rewrites anything), so
unlike blob reclaim it needs no dry-run/confirm step.

**A rejection is a record, not only a deletion** (issue #692). Rejecting a
held post or edit deletes its `posts` row, but a carried one's signed event
stays in `link_events`. The repair pass would take that as a gap and publish
the refused post again. So every rejection, local or carried, is written to
`post_rejections`: the post id (for a carried post, its event's `content_id`),
the board, who, when, and an optional reason. Every materialization path
skips a recorded id: the repair pass, and a carried post or author edit
arriving again. The signed event is kept, since local moderation never
rewrites it. A post the repair pass does restore gets the status sync would
have given it: this node's moderation, and a hold where the author's trust
requires approval. The same record is where a rejection's reason and the
author's notice come from (issue #678).

Linked resources are carried by default within the supported topology. Every
genesis a node has accepted is in exactly one recorded state (issue #561):
**carried** (a local row), **offered** (it arrived past the automatic-intake
cap `max_carried_*` and waits for the SysOp to accept it) or **excluded** (the
SysOp declined it, or deleted it while carried, and it stays out until the SysOp
restores it). Deleting a carried resource whose current origin is another node
hides it rather than destroying it: its row and content are kept but invisible
to callers and administration, not carried, and closed to new content, until
Restore un-hides it exactly as it was or Purge deletes it for real. The genesis save and its carry outcome are one transaction. The
caps bound automatic intake only: lowering one sheds nothing, a SysOp's accept
or restore is not capped, and a cap of 0 offers everything new. Offered and
excluded resources want nothing further (§8.8), are declared to peers so that
their events are not sent (issue #669), and are never visible to callers; the
SysOp sees and acts on both lists from Link status. That is how a local
exclusion is represented honestly as “not carried on this node,” not
indistinguishable disappearance.

Carrying stays automatic, and the SysOp is told (issue #681, decided with the
maintainer). A resource carried on its own arrives Uncategorized with its
origin's settings, so it is news until the SysOp looks at it:
`link_carried_to_review` records it, keyed by its Link id. The SysOp
dashboard's ATTENTION panel counts newly carried resources and offers waiting at
the cap, drawing neither count while it is zero. The console's board, file-area
and channel lists mark each unreviewed one `to review`. Opening its detail screen
is the look that clears it. A resource the SysOp accepted from an offer is not
news: they chose it.

Names are not identities in Link, and independently run nodes reuse them. A
carried resource whose name is already in use here, compared without regard
to case, is carried under that name suffixed with the first 8, then 16, then
all characters of its own id, whichever is free first (issue #671); the SysOp
can rename it like any carried resource, and peers keep seeing the genesis
name. Only with every candidate taken is it refused, as the cap refuses.

Origin recommendations never override the carrying node’s local access,
moderation, retention, or legal policy. Two origin settings are not
recommendations but authority, binding on every carrying node: closure
(§9.5) and who may post.

**Who may post** (issue #993). The board's current origin signs a
`board_posting` event naming one of three modes:

- `anyone`, the default: any node's callers post, each node's own write
  level holding its own callers;
- `origin_threads`: only the origin node's own callers start threads;
  anyone may reply;
- `origin_only`: only the origin node's own callers post, replies included.

A carried `board_post` whose author's home node (the signed
`author.home_node_fingerprint`, not the node that relayed it) is not the
board's current origin is kept as a signed event but not shown when the mode
forbids it. A reply counts as one only when its parent is on this board: one
naming a parent this node does not have would be shown as a new thread, so
under `origin_threads` it waits as a kept event until a rebuild finds its
parent. A node that is not the origin does not offer its callers `[P]ost` or
`[R]eply` where the mode forbids them, and says why; it asks again just
before a post is written, in case the mode changed meanwhile, and a door's
post there is refused before it is written. The rule follows an origin
transfer: a post the old origin wrote before the transfer and that arrives
after it is refused, since `created_at` is not authoritative (§7.4).

`board_posting` is not part of the board's lifecycle chain (§9.4). An origin
keeps only its latest lifecycle event, so a setting chained in would hide an
earlier transfer from a peer that missed it. Instead each node verifies the
event against the board's origin at the time it arrives, and the setting with
the latest `created_at` is in force; an origin transfer leaves the old
origin's setting in force until the new origin sets one. A setting signed by
a node that is not the board's current origin (a former origin's, still
relayed) is skipped on receipt, not refused, so it cannot fail the batch it
came in; a former origin drops its own setting when the origin moves, and
stops pushing it. A node with Link turned off still knows whether it is a
board's origin from the fingerprint it records at every start. The origin
keeps its own latest setting (`boards.link_posting_json`) and re-pushes and
serves it like its lifecycle event; carrying nodes keep every one in
`link_events`.

### 9.4 Origin succession

Routine node signing-key rotation is handled by the node key-transition chain
and does not transfer resource ownership.

Voluntary board-origin transfer requires mutual consent:

1. the current origin signs an offer naming the proposed new origin;
2. the proposed origin signs acceptance;
3. peers project the new origin only after both valid events.

Because this transfers authority, a SysOp picker must disambiguate peers whose
friendly/DNS presentation labels collide by showing their full fingerprints
before selection and confirmation.

Only one outstanding transfer offer is meaningful at a time.

If the current origin loses all valid signing authority and cannot publish a
transfer, the board is locally recognizable as orphaned. Existing content
remains available, but no new origin-authorized state is accepted.

A fork is a new resource/genesis with a new origin and an optional
non-authoritative `forked_from` reference. Each node independently chooses
whether to carry the original, the fork, both, or neither.

Channel-side Link lifecycle will reuse these principles after linked channels
exist; there is currently no channel genesis protocol.

### 9.5 Board closure and moderator-authorized post changes (issue #88)

Three further origin-authorized event types complete the board-lifecycle and
per-post governance surfaces left open by §9.4:

**`board_closure`.** A terminal board-lifecycle event, extending the same
`board_lifecycle_head` chain §9.4's transfer offer/acceptance already extend
— signed by the board's *current* origin, referencing the chain's current
head as `previous_event_id`. Once accepted, no further lifecycle event
(another closure, or a fresh origin-transfer offer) is accepted for that
board — closure is terminal, not reversible in this slice. Closure stops new
posts (`board_post`) to the board; it does not restrict moderator edits or
tombstones of existing content, since an archived board may still need
cleanup. Materializes locally as a `boards.link_closed_at` timestamp,
enforced by `netbbs.boards.posts.create_post` for this node's own callers and
by `materialize_carried_post` for posts carried in from other nodes (issue
#1021: before, a carried post still landed on a closed board). A post that
arrives after the closure is known is not shown, whenever it was written.

**`board_post_moderator_edit`.** Structurally identical to `board_post_edit`
(§9.2) — extends the same per-post `previous_event_id` chain — but signed by
the board's *current origin* instead of the edited post's own author's home
node, and carries no `author` field to cross-check against (the "self-authored
only" rule `board_post_edit` enforces on receipt simply doesn't apply to this
type). This is deliberately *not* a new cross-network moderator-grant
primitive: `netbbs.boards.posts.edit_post` already allows any local user
holding `BoardPermission.EDIT` to edit someone else's post (existing
behavior, unchanged by this issue) — that local permission check happens
once, on the origin node, before `netbbs.link.boards.queue_board_post_
moderator_edit_if_linked` ever builds and signs the event. A carrying
(non-origin) node's own local moderator action on a post it doesn't own
stays purely local and is never propagated — it has no origin authority to
assert an edit the rest of the network would recognize. Linked-board
moderator *grants and revocations* (a network-visible delegation of that
authority to non-origin nodes) remain out of scope, unchanged from this
section's prior framing.

**`board_post_tombstone`.** Also extends the per-post chain, as a terminal
entry — no further `board_post_edit`/`board_post_moderator_edit`/a second
tombstone is accepted once a post's chain head is already a tombstone. Same
origin-signed authorization model as a moderator edit; carries its own
placeholder `subject`/`body` (redaction content chosen once by
`netbbs.boards.posts.tombstone_post`, not reconstructed by convention on each
receiving node) plus an optional `reason`. Locally, a tombstone is a further
content-addressed revision — never an in-place mutation, and never
`netbbs.boards.posts.delete_post`'s hard delete, which stays reserved for a
still-`'pending'` post's rejection and refuses outright if any row still
references the target — so the edit chain, and any reply's `parent_post_id`,
stay intact. `posts.tombstoned_at` (nullable, plain `ALTER TABLE`, no
`CHECK`-widening table rebuild — `posts` is a live self-referencing FK parent,
and an earlier migration already documents why rebuilding it is
specifically unsafe) marks the terminal revision; `edit_post`/`tombstone_
post` both refuse to extend a chain whose current head is already
tombstoned. Requires `BoardPermission.DELETE`, no author bypass, matching
`delete_post`'s existing rule exactly.

An origin's moderator edit or tombstone of a post written by a *remote*
author is chained from that post's retained events: a carried revision keeps
its signed event only in `link_events`, never in `posts.link_event_json`
(issue #677; until then such posts looked off-chain and the origin's
moderation of them was never sent).

**A local tombstone is terminal on the node that made it** (issue #677). A
carrying node's moderator may remove a post locally even though the removal
is never propagated. A `board_post_edit` or `board_post_moderator_edit`
received afterwards is retained as a signed event, so relay and inventory
are unaffected, but it is never projected over the tombstone. Otherwise a
later revision would sort above the tombstone and bring the removed content
back. Removing or editing someone else's post on a non-origin node therefore
changes this node's copy only, and the moderator is told so: the confirmation
asks to remove the post "on this node only", and the outcome says that other
nodes carrying the board keep the original. On the origin, the confirmation
still says the removal cannot be undone. A closed board offers callers no
`[P]ost` action and says it is closed, rather than letting a caller write a
post that `create_post` would then refuse.

All three share `board_origin_transfer_offer`'s verification shape: resolve
the board's current origin (`current_board_origin`, not the genesis's
original claim), confirm it's an independently-known peer, verify the
signature against its current signing key. None invents a new authorization
primitive — they reuse the origin's existing signing identity and, for the
two post-scoped types, an already-existing local permission check.

Still future or incomplete:

- linked-board moderator grants and revocations (delegating origin-recognized
  moderator authority to a non-origin node, rather than only the origin
  itself ever asserting a moderator edit/tombstone, as above);
- general public-network anti-entropy beyond the current bounded
  full-known-ID inventory exchange;
- Link-blanket governance surfaces and audit feeds (Phase 6).

### 9.6 Linked channels (issue #87)

Mirrors §9.1-§9.3's promotion/genesis/carry model as closely as possible
rather than inventing a parallel one, with two differences that follow
directly from how local channels already differ from local boards, not
from anything specific to Link:

**No edit chain.** Local channel messages have no edit concept at all
(chat access is participate-or-not, no read/write split, §5.4) — there is
no `channel_message_edit` mirroring `board_post_edit`, because there is
nothing locally to mirror. A `channel_message` is immutable, single-shot
content exactly like a `board_post` with no reply/parent structure (chat
scrollback is flat and chronological, never threaded).

**Origin succession is reused by reference, not reimplemented in this
issue.** §9.4's mutual-consent transfer/orphan/fork model applies
unchanged if a channel ever needs it — the same signed-offer-then-signed-
acceptance shape, the same "at most one outstanding offer" rule, the same
"orphaned means no new origin-authorized state, existing content stays"
behavior for a channel whose origin loses all signing authority. This
issue does not add `channel_origin_transfer_offer`/`_accepted` event
types — genesis/promotion/materialization/messages are the actual scope
(the "Recommended direction"'s own list), matching how governance is
explicitly deferred to Phase 6 rather than half-built here. `channels`
gains no `link_lifecycle_json` column in this issue for the same reason
— add it alongside the transfer event types themselves, when and if a
future issue actually needs it, rather than carrying an unused column now.

**Event family.** Two new object types, `channel_genesis`/
`channel_message`, structurally identical to `board_genesis`/`board_post`
minus the fields above:

- `channel_genesis`: `origin_fingerprint`, `channel_id` (the *existing*
  local content-addressed `channel_id`, never newly minted — same "promote
  an existing local resource" rule §9.1 already states for boards), `name`,
  `created_at`, and optional cascading-recommendation fields mirroring
  `Channel`'s own settable columns (`description`, `min_level`, `min_age`,
  `name_requirement`) — no `default_min_write_level`/`default_moderated`/
  `default_max_post_age_days` equivalents, since `Channel` has none of
  those to recommend a default for. Signed by the origin's current signing
  key, same as `board_genesis`. One per `channel_id`, ever — a different
  genesis for the same `channel_id` is a conflict, identical to
  `has_conflicting_genesis`'s existing rule.
- `channel_message`: `channel_id`, `author` (the same tagged union
  `board_post` uses — only `node_vouched_user` has a real build/verify
  path today, for the same reason), `body`, `created_at`, `nonce`. No
  `subject` (channel messages don't have one) and no
  `parent_post_id`/reply structure (scrollback is flat).

Canonical encoding, event-identity, and verification follow §7.2-§7.4
exactly as already specified for every other event family — no new rule
needed. `handle_events` gains two new branches following the identical
shape `board_genesis`/`board_post` already use, including issue #85's own
verify-against-claimed-origin-not-wire-sender fix from the start (no
separate "add multi-hop later" step this time — channels get it on day
one, since #85 already generalized `handle_events`'s verification model
before this issue landed).

**Carry and materialization.** A peer accepting a valid `channel_genesis`
materializes a real, locally browsable `Channel` row —
`materialize_carried_channel`, mirroring `materialize_carried_board`'s
exact shape: bypasses `netbbs.chat.channels.create_channel` (which mints a
fresh content-addressed ID from the *local* creator/timestamp, wrong for
carried content), inserts directly using the genesis's own `channel_id`
verbatim, seeds settings from the genesis's cascading recommendations, and
is idempotent (a resend, or a second peer relaying the same genesis,
returns the existing row unchanged).

A `channel_message` materializes into an ordinary `channel_messages` row
— `materialize_carried_channel_message`, using `netbbs.chat.scrollback`'s
existing insert-and-trim shape (`record_message`'s own logic, not a
parallel path), keyed by the event's own `content_id` as the row's local
identity for dedup purposes the same way a `board_post`'s `content_id`
becomes its local `post_id`. `author_label` follows the same
`local_user_id@home_node_fingerprint` synthesis `materialize_carried_post`
already uses; no local account is implied.

**A real, worth-stating consequence of reusing the existing bounded
scrollback rather than inventing unbounded storage for channel content:**
`channel_messages` is a trimmed, bounded scrollback by local design
(`netbbs.chat.scrollback`'s own configured limit, default 100), not a
permanent archive the way `posts` is. A materialized linked message is
subject to the exact same trim as a local one — old linked-channel history
ages out of scrollback precisely as old local history already does. This
is a deliberate consequence of treating a linked channel as genuinely the
same kind of resource as a local one (bounded live chat), not a silent
data-loss surprise unique to Link: the identical bound already applies
today to every channel's own local messages. A self-originated message
queued for push (`channel_messages.link_event_json`, the messages-table
counterpart to `posts.link_event_json`) that gets trimmed before any sync
pass ever pushes it is simply never propagated over Link — bounded,
honestly-scoped, matching this project's own "explicit bound, defined
behavior" principle rather than an indefinite queue.

**Idempotent duplicate delivery, restart reconstruction, and inventory
serving** all follow the identical shape §9.3 and §8.8 already establish
for boards: `channels.link_genesis_json` (new nullable column) is the
restart-safe source for a carried channel's genesis, read unconditionally
the same way `boards.link_genesis_json` already is; `channel_messages.
link_event_json` mirrors `posts.link_event_json` for self-authored
tracking. Issue #85's inventory diff extends to `channel_id`-scoped
`link_events` rows the same way it already covers `board_id`-scoped ones.

---

## 10. Link messages

### 10.1 Product model

A Link message extends the ordinary local mailbox. The user composes to a
`user@node` address (§4.4) and reads the result in the same inbox/sent UI
as local mail. The To prompt checks a Link address as it is typed, the way it
checks a local name, and asks again in place with what to type instead: a
malformed address, a node this BBS is not linked with, a name more than one
linked node goes by, and a peer this node will not send mail to (§12.4) are
all refused there, never after the message is written. Send repeats the
checks, because the review screen's `[T]o` can change the address. Once
resolved, the address is kept by the node's technical identity, not the name
typed (issue #826).

The message is point-to-point to one recipient node, not flood-filled public
content.

### 10.2 Confidentiality guarantee

The implemented confidentiality tier is `tier1_home_node_key`:

- subject and body are encrypted to the recipient node’s current signing-key
  material converted to X25519;
- network peers and relays cannot read the content;
- the recipient’s home-node operator technically can decrypt it.

This is not end-to-end encryption against the home node and must not be marketed
as such.

The X25519 key is derived from the existing Ed25519 key through libsodium’s
supported conversion. This deliberately couples encryption and signing-key
rotation and accepts the larger compromise blast radius in exchange for a much
simpler lifecycle and no separate key-distribution protocol.

Static recipient keys do not provide forward secrecy. A later compromise can
expose previously captured messages.

`tier2_personal_key` remains reserved vocabulary but is permanently out of the
planned product scope unless NetBBS gains a real client-side decryption
architecture. Server-side terminal sessions cannot render content which the
server is forbidden to decrypt, and a web-only feature is not sufficient to
justify a parallel mail system.

### 10.3 Delivery state

Transport receipt is not user delivery.

Separate signed events represent:

- accepted into the recipient mailbox;
- bounced because of unknown recipient, full mailbox, blocking, a letter the
  recipient node cannot decrypt, a malformed letter, a recipient that takes no
  mail (`no_mailbox`: the node's shared guest account, issue #816, §6.4), an
  account that takes none at the moment (`recipient_unavailable`: disabled or
  awaiting approval, not saying which, issue #818, §6.4), or another defined
  terminal failure. Blocking has two codes:
  `blocked_sender` is the recipient *node's* trust policy refusing the
  sender or its node (§12.4), and `blocked_by_recipient` is the recipient
  *person* having blocked this sender (issue #817, §6.4). The sender is told
  which: "that BBS does not accept mail from you or from this BBS" against
  "the recipient does not accept mail from you";
- future expiry where retry policy requires it.

Outbound messages remain pending until an accepted or bounced event arrives.
Delivery through a relay does not change the acceptance semantics. One answer
is not a signed event: a recipient node whose trust policy refuses a direct
push says so with HTTP 403 and a `link_policy_*` reason code, and the sending
node records that as a bounce, since asking again would get the same answer,
unless another of the recipient's addresses or relays takes the message
(§12.4, issue #804).

The sending node shows the state to the sending user (issue #806). Sent marks
each Link message pending, with relay (§10.3, below), delivered, bounced or
expired, and the message's
Delivery line gives a bounce's reason in plain words. The reason is the signed
bounce's `reason` or the refusal's `link_policy_*` code, stored with the
message (at most 64 characters, since another node chose it); a code this
node does not know reads as "that BBS refused it". Expired means the delivery
work item dead-lettered: no route took the message, or this node's own trust
policy held it back to the end, which is recorded as its own reason
(`own_policy`), or that a letter left at a relay got no answer in time
(`no_answer`, below). A bounce
or expiry flags the message until its sender is told: once, at their next
main menu, which covers a sender who was offline when it happened, or by
opening it in Sent. Mail the sender already deleted from Sent is not told
about. A later acceptance clears the flag and wins; a later bounce wins too,
and flags the message again unless it was already bounced. A bounce
message in the inbox would need a system sender (issue #819) and is not sent.

The recipient node checks everything the sending node chose where it enters,
in `deliver_link_message` (issue #808), and answers each failure with a
signed bounce rather than an exception that would lose the letter in silence:

- the sender's `local_user_id` must match the address grammar
  (`[A-Za-z0-9_.-]{1,32}`, §4.4), because it becomes the address a reply goes
  to. A NetBBS node only sends names its username rules allow; the one honest
  exception is an account older than those rules on a node older than #807,
  and a reply to it could not be addressed either. Anything else bounces
  `malformed`;
- the decrypted letter must be a JSON object whose subject and body are text
  within the limits local mail keeps (a subject that is not blank, 200 bytes;
  a body of up to 20,000 bytes; both encodable as UTF-8). Anything else
  bounces `malformed`;
- a ciphertext sealed to none of this node's current or retired signing keys,
  or not base64 at all, bounces `undecryptable`. It used to bounce
  `unknown_recipient`, which sent the sender looking for a typo.

`undecryptable`, `malformed`, `no_mailbox`, `blocked_by_recipient` and
`recipient_unavailable` are bounce reasons added after v7.13.0. Every
earlier release keeps a received bounce's reason without checking it (v7.13
and older ignore it entirely and just mark the message bounced), so a new code
reaches an older sender as a plain bounce and costs it nothing but the
wording.

A received letter is dated by its signed `created_at`, when its sender wrote
it, so a letter that took days to arrive says so. A `created_at` more than
five minutes ahead of the recipient node's clock (the skew Link's signed
requests allow), one before 2000 (no NetBBS node wrote mail then, and a date
near year 1 cannot be shown in a timezone west of UTC), or one that is not a
timestamp, is replaced by the arrival time: the sender's clock cannot put a
letter in the future or out of range. Inbox and Sent
list mail by arrival (row id), not by that date, so late mail lands at the top
of the inbox instead of below letters read long ago; making room in a full
mailbox likewise removes the earliest-arrived read letter.

A letter left at a relay is not delivered on the relay's word (issue #874).
The relay is not the recipient, and some answers never come back: a recipient
node that holds the sending node quarantined or blocked refuses such mail at
pickup without a bounce (§12.4), and an acknowledgement can be lost. So the
sending node records when it handed the letter over
(`mail_messages.link_relay_handoff_at`); the letter stays `pending`, and Sent
shows it as "with relay" ("With a relay, no answer yet") rather than plain
pending. Each sync pass expires a letter still pending 14 days after its
handoff, with reason `no_answer`, and its sender is told as for any expiry,
in words that say it may have arrived all the same: no answer came back. An
answer that arrives later still wins, as above. The timeout is the sending
node's alone: nothing changes on the wire, and it works the same with old
relays and old recipients. Mail pushed directly to the recipient never times
out this way, since the push itself reached the recipient's node; mail left at
a relay before the upgrade that added the handoff time cannot be told apart
from it and keeps waiting.

A relay must hold a deposit longer than this timeout. Any limit on how long a
relay mailbox keeps an uncollected deposit (30 days since issue #891) must stay
well above 14 days, so that a recipient that is merely slow to collect still
answers inside the sender's window; a limit below the timeout would let a
letter vanish while its sender still reads "no answer yet".

### 10.4 Routing limitations

Direct delivery requires a known peer with a usable endpoint. An outgoing-only
recipient may be reached through relays it has selected and published.

The current system does not introduce total strangers. The sender must already
know enough authenticated peer/key state to encrypt to the destination, and the
recipient must know enough sender state to verify the message.

### 10.5 Metadata and abuse controls

Only routing information needed by transport or relay infrastructure should be
visible outside the encrypted body. Subjects and bodies remain encrypted.

Mailboxes, relay storage, retries, and pending acknowledgements are bounded.
Blocking and quota failures must be explicit; unread data is not silently
removed to make delivery appear successful.

### 10.6 Tier-2 recipient scope (issue #90) — deferred, not permanently

**Not to be confused with `tier2_personal_key` (§10.2).** That is a
different, already-decided, permanently-out-of-scope concept: client-side
end-to-end encryption against the home node itself, blocked by a hard
architectural constraint (no client-side decryption exists). This section
is only about *recipient reachability* — delivering `tier1_home_node_key`
messages to a peer the sender has never directly, verifiably completed a
hello with — which has no equivalent hard blocker, just isn't built yet.

**What §10.4's "no total strangers" boundary actually rests on.** A
`HelloMessage` bundle is *self-authenticating* by construction (§12): a
peer that didn't hold the claimed root key's private half could not have
produced a transitions chain that both verifies against that root and
whose resolved current signing key matches the descriptor's own
signature. Verifying someone's identity has never required trusting
*who* handed you the bundle — only the bundle's own internal
cryptographic consistency. Today's requirement that this always happens
via a completed two-way hello is a stronger condition than the
verification itself actually needs.

**Neither issue #85 nor issue #630 provides tier-2 reachability.** Relay
(#85) carries content between nodes; introduction (#630, §8.11) lets a
receiving node verify content signed by a node it has never met, by
fetching that node's self-certifying bundle from the carrier. Neither
reaches mail. An introduced identity is kept apart from peers precisely so
that it cannot be addressed, pushed to or pulled from, and a carrier
answers only for nodes whose content it has just served or that it relays
for (§8.11), which a mail recipient need not be.

**What a real tier-2 design would require, concretely.** The bundle
exchange exists (§8.11) and verifies a bundle exactly as if its subject had
dialed in directly, whoever served it: a tampered or fabricated bundle
simply fails verification. Still missing for mail: a way to ask for the
identity of a *recipient*, who has signed nothing the asker was served; the
same in reverse for the recipient verifying the sender; a delivery path,
since a node known only by introduction was never dialed and may not be
dialable; and the decision to let an identity learned that way be
encrypted to, with no other change to §10.2's confidentiality model.

**Confidentiality and abuse implications, if built.** No new
confidentiality exposure to message *content* — encryption still targets
the real recipient's real key, independently verified, regardless of who
relayed the bundle that made verification possible. The exposure is
metadata: a relay learns that someone is asking about a specific
fingerprint, an availability/traffic-analysis concern rather than a
confidentiality break of any message body. A dishonest relay can only
withhold or refuse to relay a bundle (availability), never forge one
(self-certification), matching the same "worth trying, never blindly
trusted" property peer-list exchange already established for addresses.

**Decision: deferred, not scoped as active work.** Unlike
`tier2_personal_key`, this is not a permanent non-goal — there is no
architectural blocker, only that it is not needed to unblock or validate
current work. Revisit if a real deployment need appears (e.g. issue #83's
dogfood run surfaces callers who actually want to message someone they've
never directly synced with) rather than building it speculatively now.

---

## 11. Remote file areas (issue #89)

A linked file area remains owned and stored by its source node — unlike a
linked board, where a carrying node eagerly materializes a full local copy of
every post (§9.3), file content is deliberately **not** eagerly replicated:
bytes can be large, so they are fetched on demand, in bounded resumable
chunks, only when a local user actually wants one. The catalogue (what files
exist, their names/sizes/hashes) is still gossiped and fully browsable
without ever fetching any content — mirroring §9's promotion/genesis model
for the metadata half, then adding one genuinely new mechanism (chunk
transfer) for the content half that boards/channels never needed.

### 11.1 Promotion and genesis

An existing local file area may be promoted into Link scope exactly like a
board (§9.1): one signed `file_area_genesis` referencing the existing stable
`area_id`, never a newly minted one. `file_areas` gains `link_genesis_json`/
`link_origin_fingerprint` columns mirroring `boards`' own pair exactly (the
table's pre-existing `origin_node_fingerprint` column is unrelated dead
Phase-1/2 scaffolding, left untouched, the same precedent `boards.
origin_node_fingerprint` already set — a fresh column, not a repurposed old
one). No `link_lifecycle_json`/origin-succession event types in this issue —
same deliberate deferral §9.6 already applied to channels, for the same
reason: genesis, catalogue, and transfer are the actual scope; add transfer
event types alongside a future issue that actually needs them rather than
carrying an unused column now.

`file_area_genesis` payload: `origin_fingerprint`, `area_id`, `name`,
`created_at`, and optional cascading-recommendation fields mirroring
`board_genesis`'s own shape (`description`, `default_min_read_level`,
`default_min_write_level`, `default_moderated`, `default_max_file_age_days`,
`default_min_age`, `default_name_requirement`) — one per `area_id`, ever,
same conflict rule §9.1 already states.

### 11.2 File descriptors (catalogue, no content)

`file_descriptor`: the catalogue entry for one file, gossiped the same way a
`board_post` is, but describing metadata only — `area_id`, `file_id` (the
existing local content-addressed id, computed the same way
`netbbs.files.entries.upload_file` already computes one), `filename`,
`description`, `size_bytes`, `sha256`, `created_at`. No `parent_post_id`
(files aren't threaded); no `author` tagged union the way `board_post` has
one — attribution is a local admin-log concern (§18), not something a
catalogue entry needs to assert network-wide, and an uploader's identity
carries no bearing on whether a peer should fetch the bytes. Only an
`area.moderated`-approved, locally-uploaded file is ever queued as a
`file_descriptor` — the identical "never leak a moderation queue onto the
network" rule §9.2 already states for `board_post`. Immutable, single-shot,
like `board_post`/`channel_message` — no edit chain; a changed file is a new
upload with its own new `file_id`, not a revision of an old one. Immutable is
not the same as permanent: see *Withdrawal* below.

Queued from exactly two places, mirroring `board_post`'s own pair (issue #464,
which fixed a period where neither existed and a Linked area consequently
announced nothing it held): the upload itself, once the transfer finishes, and
the SysOp's pending-file approval, for a moderated area. That split is what
implements the rule above, rather than restating it — the upload-side call is
simply a no-op while the file is `'pending'`.

Only the area's own origin ever queues one. A carried area is a real, writable
local area, but a peer verifies each `file_descriptor` against the signing key
of the area's genesis origin, so a descriptor signed by anyone else verifies
nowhere; unlike a `board_post`, which carries an author tier precisely so it
can travel from any node into a carried board, describing a file is the
origin's act. An upload into a carried area is stored, listed and downloadable
locally, and never enters the catalogue peers see.

A receiving node's catalogue materialization is genuinely different from a
carried board's: the *area* becomes a real local `FileArea` row (browsable
via the ordinary `list_file_areas`), but an individual `file_descriptor`
does **not** become a `files` row — `netbbs.files.entries`' own invariant
("a file row is only ever created after its bytes are already safely
written to storage") stays true unconditionally, so a catalogued-but-not-
yet-fetched file cannot live there. It lives instead in a new `remote_files`
table (`file_id`, `area_id`, `origin_fingerprint`, `filename`, `description`,
`size_bytes`, `sha256`, `created_at`, `link_event_json`, `fetched_file_id`)
— catalogue metadata only, browsable and listable (the acceptance
criterion's own "discover and list... without fetching any file content"),
with `fetched_file_id` set only once §11.3's transfer completes and
verifies, at which point the content is promoted into a genuine `files` row
indistinguishable from a local upload for browsing/download purposes.

**Withdrawal — a catalogue entry may outlive its file, and stops when it is
asked to (issue #479).** Once an area announces its uploads, a peer's
catalogue can describe a file its origin no longer has: the SysOp deleted it,
or `_sweep_expired_files` purged it past the area's grace period. Either way
the only origin row that could serve the bytes is gone, and nothing in the
catalogue knows.

The origin says so when asked. A chunk request (§11.3) for a `file_id` this
node holds no row for is answered **HTTP 410 with a signed `file_withdrawal`**
— `file_id`, `requester_fingerprint`, `transfer_id`, `request_nonce`,
`created_at`, `nonce`, signed by the origin's **current** signing key. That is
the same origin *identity* that signed the `file_descriptor` being withdrawn,
but not necessarily the same *key*: a descriptor is immutable and keeps the
signature it was created with, while an operational signing key rotates (§12).
An implementation that verified a withdrawal against the descriptor's own key
would reject every legitimate one issued after a rotation; the current key is
resolved through the origin's transition chain, as everywhere else.

Acting on one is irreversible in a way discarding a bad chunk is not: once the
`remote_files` row is gone, the `file_descriptor` still in `link_events` means
ordinary redelivery will not bring it back. So the requester checks four things,
all of them before deleting anything:

- **the envelope is a `file_withdrawal`.** The descriptor being withdrawn is
  gossiped to the whole mesh and carries the very same `file_id` — and until the
  origin rotates its signing key, is signed by the very key a withdrawal is
  verified against, which is exactly when the confusion is exploitable — so "signed by the origin, names this file" describes a
  document any interceptor already holds, and `object_type` is the only thing
  separating the two. It is a precondition of the signature check, not a
  formality;
- **`requester_fingerprint` is this node, `transfer_id` is this fetch, and
  `request_nonce` echoes the chunk request's own authorization nonce.**
  `transfer_id` is content-derived from `(file_id, requester)` and so is
  identical across retries — it names the transfer, never the individual
  request. `request_nonce` is what makes the answer single-use, so a withdrawal
  captured off the wire cannot be replayed even at the same node for the same
  file;
- **`created_at` is fresh**, on the same five-minute window and for the same
  reason `InventoryRequest` has one: a signature is durable, and without
  freshness a recorded 410 stays usable indefinitely — including after the
  origin restores the file from backup, when the entry it deletes would describe
  bytes the origin is serving again;
- **the signature verifies** against the origin's current signing key — and an
  envelope that cannot be canonicalized fails this check rather than raising,
  since it could not have carried a valid signature anyway.

Any failure refuses the withdrawal and changes nothing. On success the requester
deletes its `remote_files` row along with the fetch state that existed only to
serve it (`link_file_transfers`, its chunk records, and any staging file), and
tells the caller the origin no longer has the file rather than reporting a
generic transfer error. A chunk still in flight for a transfer withdrawn this
way fails as an ordinary `FileTransferError` rather than writing into a row that
no longer exists.

A withdrawal is **not** a gossiped tombstone, and deliberately so. A
`file_descriptor_tombstone` mirroring `board_post_tombstone` would have to be
retained and re-offered forever — one per deleted file, and one per file every
expiry sweep purges, growing without bound in exactly the place §8.8's push
direction had just finished bounding. A point-to-point answer to a
point-to-point request covers deletion and expiry alike with no retained state
anywhere, in the same never-gossiped, never-through-`handle_events` shape
`file_chunk_descriptor` already uses.

The cost is stated rather than hidden: **a stale entry survives until somebody
tries to fetch it.** Listing is honest about what the origin last announced,
not about what it still holds; the first attempt is what reconciles them, and
it reconciles them permanently. The event dedup that makes `handle_events`
idempotent is what keeps a re-delivered `file_descriptor` from resurrecting a
withdrawn row.

An entry whose `fetched_file_id` is already set is never withdrawn. Those
bytes are local, verified and promoted into a real `files` row; the origin
dropping its own copy is not a reason to un-list this node's.

### 11.3 On-demand chunk transfer

Unlike every other Link mechanism so far, chunk transfer is a direct,
point-to-point pull against one specific peer — the file's own
`origin_fingerprint` (§11.2), never relayed, never gossiped, and not part of
`handle_events`'s dispatch at all: nothing here is a candidate extension of
a shared chain the way board/channel events are, so there is nothing to
verify against a "current origin" the way `board_post_edit` does.

**Wire shape.** A new pair of HTTP routes (`netbbs.link.transport`),
alongside `/events`/`/inventory`: `POST /link/v1/file-chunk/{fingerprint}`.
The **request** is a small, unsigned JSON bundle (mirroring
`InventoryRequest`'s own "not a candidate chain extension, nothing to sign"
reasoning) — `transfer_id`, `file_id`, `chunk_index`, `max_chunk_size`. The
**response** carries the chunk's raw bytes as the literal HTTP body — never
base64-embedded in JSON, per the issue's own explicit requirement — plus a
*signed* `file_chunk_descriptor` (`file_id`, `chunk_index`, `chunk_sha256`,
`chunk_size`, `total_size`, `is_last`, `created_at`), delivered in a response
header (`X-NetBBS-Chunk-Envelope`, base64 JSON) rather than the body, so the
body stays purely raw bytes. Signed by the origin's current signing key —
the same `_resolve_sender_signing_key` resolution every other Link
verification already uses — so a requester can verify the chunk's
authenticity and integrity (`chunk_sha256` against the actual bytes
received) independent of transport-level trust, the same "objectively
verifiable" standard §12.3 holds every piece of Link content to.

**Requester side is required to already be a completed peer** of the
origin (unlike `/inventory`, which serves already-gossiped small metadata to
anyone) — serving arbitrary bytes to an unauthenticated caller is a new
resource-exposure the metadata-only routes don't have, so this route
requires the same "no relay from a stranger" hello precondition
`/events` already enforces.

**Deduplication and resume.** `transfer_id` is deterministic — a content
hash of `(file_id, requester_fingerprint)` — so a retried or resumed fetch
naturally reuses the same id rather than minting a new one every attempt;
the origin uses it to bound concurrent transfers per requester (§13.5's
bounded-remote-influence principle: an explicit `max_concurrent_file_
transfers`-per-peer cap, visible rejection once exceeded, never silent
unbounded growth). `chunk_id` is the chunk's own `sha256` — an exact-content
dedup key, not a sequence number alone: a resent identical chunk (the same
request repeated, or a resumed transfer re-requesting a chunk it turns out
it already has) is recognized as already-applied and skipped rather than
re-written, the same idempotent-resend discipline every gossiped chain
already applies, just against a `link_file_transfer_chunks` row instead of
`known_event_ids`. `link_file_transfers` tracks one row per transfer
(`transfer_id`, `remote_file_id`, `total_size`, `chunk_size`,
`bytes_received`, `status`, staging path) — resuming an interrupted transfer
means asking for the next chunk index past what's already recorded, nothing
more elaborate. Once every chunk is received, the reassembled content's
sha256 is verified against the file's own catalogued claim (§11.2) before
promotion into real storage — a peer claiming a `file_descriptor` with a
hash that doesn't match what it actually serves is refused at that point,
never silently accepted.

**Completed content is stored once by hash.** The verified reassembly is
handed to `netbbs.files.storage.move_temp_file_into_storage` — the exact
same content-addressed layout local uploads already use, so a file fetched
from two different catalogues (or already locally uploaded, coincidentally
identical bytes) shares one stored blob automatically, no special-casing
needed; `remote_files.fetched_file_id` then references the resulting real
`files` row.

**Not a generic work-item/DLQ instance.** Chunk transfer is deliberately
*not* folded into issue #60's outbound-retry abstraction (§13.7) — like
board/channel gossip (§13.7's own "not every retry-shaped mechanism fits"
lesson), it already has a natural resumable-by-construction terminal state
(`link_file_transfers.status`) and no correct "give up" state distinct from
"the requester stopped asking"; a second, differently-shaped retry
abstraction bolted on top would only compete with that.

**Explicitly out of scope for this issue** (mirroring §9.6's own precedent
for channels): file-area origin succession (reused by reference, §9.4's
model, if ever needed); write-back/uploading to a remote area (§11 itself:
"remains owned and stored by its source node"); public/untrusted file
discovery (Phase 4). Inventory/pull catch-up (§8.8) extended to file areas
was left out of this issue too, but is no longer an open gap — see §11.4
(issue #93).

### 11.4 Inventory/pull-based catch-up for file-area catalogues (issue #93)

§8.8's `InventoryRequest`/diff mechanism extends to file-area catalogues
the identical way §9.6 already extended it to channels: a third key,
`file_areas`, alongside `boards`/`channels`, keyed by every `area_id` this
node currently carries (bounded by its own `max_carried_file_areas` quota,
same "request size already bounded by an existing cap" reasoning §8.8
states for boards) mapped to the full set of content IDs already known for
it. The responder's diff (`netbbs.link.store.file_area_event_diff`) unions
the same three sources §8.8/§9.6 already established for boards/channels:
this node's own self-originated genesis (`file_areas.link_genesis_json`,
never routed through `handle_events`), any `file_descriptor` a *local* user
queued regardless of whether this node originated or merely carries the
area (`files.link_event_json`, populated only by self-authorship, per
`netbbs.link.files.queue_file_descriptor_if_linked`'s own scope), and every
peer-received event this node has accepted (`link_events`, filtered by a
new `file_area_id` column, the file-area-scoped counterpart to `board_id`/
`channel_id`). `_handle_inventory` shares one overall `_MAX_EVENTS_PER_
REQUEST` budget across all three diffs now — board, then channel, then
file area, each with whatever remains — not three independent caps.

Only catalogue metadata (`file_area_genesis`/`file_descriptor`) is ever
recoverable this way — this section changes nothing about §11.3's chunk
transfer, which stays a direct point-to-point pull the requester still
must initiate explicitly against the file's own origin once it learns of
a descriptor. A node that recovers a missed `file_descriptor` through an
intermediary carrier therefore ends up with a real, browsable catalogue
entry (`remote_files`, `fetched_file_id` still `NULL`), not fetched bytes
— turning inventory into automatic content replication was explicitly out
of scope for this issue, matching the acceptance criteria's own "recover
metadata/catalogue divergence only" framing.

`file_area_genesis` already went through `netbbs.link.store.save_event`'s
generic dispatch (the same path `board_genesis`/`channel_genesis` use);
`file_descriptor` does not (`materialize_carried_file_descriptor` inserts
its own `link_events` row directly, same shape `materialize_carried_post`/
`materialize_carried_channel_message` already established) — so populating
the new `file_area_id` column needed two call sites, not one, unlike
`channel_id` (only `channel_genesis` needed it, since `channel_message`
does its own insert too, but happened to need no scoped column of its own
until this issue). Restart reconstruction needed no new code: `node.
file_areas` (via `FileAreaEventState`) was already rebuilt from both
`file_areas.link_genesis_json` and `link_events` by issue #89's own
`load_link_node` changes, and `file_descriptor` has no chain state beyond
`known_event_ids`/`events` to rebuild in the first place — the same "no
branch needed" reasoning issue #89 already documented for descriptors.

---

## 12. Trust, reputation, probation, and quarantine

Phase 4 defines the public-network security model. This section is the
normative threat model and policy contract. It does not make a Phase-3 build
safe for public federation by documentation alone; the persistence,
enforcement, operator UI, and tests described here must exist first.

### 12.1 Security goals and attackers

The model protects a node's availability, storage, users, and local policy
without creating a network-wide authority. It must remain safe when facing:

- a malicious user acting through an honest home node;
- a malicious node which signs abuse, lies, selectively relays, or vouches for
  abusive users;
- Sybil identities and colluding nodes;
- a compromised established node or configured trust reporter;
- replaying, withholding, reordering, or selectively forwarding intermediaries;
- partitions, clock skew, stale observations, and compromise recovery.

A signature proves key control, not honesty or independence. A successful dial
proves reachability, not trust. Seeds, discovery, peer introduction, relay
consent, and carrying the same resource confer no reputation.

Every enforcement decision remains local. Nodes may disagree and may override
automatic policy. No signal can force another operator to hide, delete, relay,
or accept anything.

### 12.2 Separate trust dimensions

NetBBS does not compute one scalar reputation score. A local trust view keeps
these dimensions separate:

- **identity and protocol integrity:** signatures, key lifecycle,
  canonicalization, equivocation, and authorization;
- **resource behavior:** flooding, quota evasion, retries, availability claims,
  and relay/storage use;
- **content conduct:** spam, harassment, illegality, off-topic behavior, and
  other moderation judgments;
- **operational reachability:** this node's own dial outcomes.

Operational reachability is routing data only. `netbbs.link.reliability` may
rank dial or relay candidates but must never affect security or content trust.

Node and user trust are separate. Establishing a home node does not establish
every user on it; one abusive user does not quarantine an otherwise honest home
node. A password-only `node_vouched_user` is evaluated as the stable pair
`(home_node_fingerprint, opaque_local_user_id)`, never by display label.

Remote identity attestations have a separate local trust list. Trust-report
authority does not grant age/name-attestation authority, or vice versa.

### 12.3 Local roles, states, and precedence

Establishment and authority to influence policy are different roles:

- a **trust anchor** is explicitly configured by the SysOp;
- an **established identity** has graduated or was established manually;
- a **trusted reporter** is explicitly configured for named dimensions and
  categories. `dimension:*` is shorthand, expanded when the grant is saved into
  every category of that dimension the node's version knows; a category added
  by a later version is never granted implicitly. A category the version does
  not know may still be named (its signals are retained without effect), and
  the console says so when the grant is saved (issue #745);
- a **trust domain** locally groups reporters which may share control or
  incentives.

No role is inferred transitively. A vouch may help probation graduation but
cannot create a trust anchor, reporter, attestation authority, or trust domain.
When a SysOp selects a node by friendly or DNS name for one of these
security-policy roles, the UI shows the resolved technical identity. If that
fingerprint has an undismissed cryptographic-identity warning, the warning is
shown and explicit confirmation of that technical identity defaults to no
before policy can change. Entering the complete fingerprint already supplies
that confirmation.

Per identity and trust dimension, local state is `probationary`, `established`,
`quarantined`, or `blocked`. Manual block has highest precedence, followed by
explicit SysOp overrides, automatic quarantine, then ordinary probation or
establishment. Overrides are scoped, reasoned, timestamped, and audited. Node
sovereignty permits overriding even self-verifying evidence, but the UI must
keep the evidence and risk visible.

### 12.4 Probation and vouching

A remote node starts probationary. Default automatic graduation requires:

- 30 elapsed days since the first verified hello;
- verified direct interaction on three distinct UTC dates;
- no active local integrity or resource trigger;
- active vouches from two trusted reporters in two trust domains.

A remote user starts probationary independently. Default graduation requires:

- 14 elapsed days since first accepting that stable user identity;
- accepted activity on three distinct UTC dates;
- no active trigger in any applicable dimension;
- one authorized user vouch, or explicit SysOp establishment.

A node's day of direct interaction is a hello completed in either direction or
a push from it that this node accepted. A user's day of activity is an
accepted event they authored, whichever node delivered it. Content a carrier
brings is not an interaction with the node that originated it. Each counts once
per UTC date, and only where trust policy is enforced (issue #1035; before it,
nothing recorded these days and no subject graduated automatically). A subject
quarantined or blocked in any dimension banks no days, and only days after its
most recent quarantine or block count toward graduation: recovery returns to
probation, and probation starts its count again.

A home node's identity vouch binds an opaque user ID to that node; it is not a
behavioral vouch. Probation does not follow a changed home node or signing
identity without a future signed identity-transition protocol.

By default, probation is read-only from the subject's perspective. A
probationary node may complete hello/key-lifecycle exchanges and make bounded
inventory pulls at one quarter of the established-peer request budget. This
node may accept through it content independently signed by an established
author, but refuses or holds for explicit local approval new content authored
or node-vouched by the probationary identity. A probationary node contributes
no trust-signal weight and is not selected to serve as a relay. A probationary
user's posts/uploads enter applicable local approval flow. Private operators may
establish a known node manually instead of waiting for automatic graduation.

Probation is shown to the SysOp in both directions (issue #844), because a
new node otherwise cannot tell "working, just new" from "broken". Held-back
content is counted and named per node from the running node's set-aside list.
Whether a peer holds this node on probation is learned from the policy 403 on
this node's own push, and whether it holds a linked resource from that push or
from an inventory exchange that declared the resource and did not ask for it.
Both are kept in memory and known only for peers this node dials; a peer that
only dials this node reads as unknown rather than guessed.

**Node trust covers Link mail (issue #804).** A `link_message` is private mail
to one recipient, not publication, so user probation does not gate it: a
message from a user whose home node is established here is delivered even
while that user is still probationary. The sender's home node must be
established; a message from a node still on probation, or from a quarantined
or blocked user or node, is refused, and never silently. A direct push is
refused with the policy 403 and its reason code, which the sending node
records as a bounce rather than retrying, once none of the recipient's other
addresses or relays took the message (the 403 is unsigned, and a stale address
now answered by another node refuses the same way). Mail picked up from a
relay mailbox has no synchronous answer, so the refusal becomes a signed
`link_message_bounced` with reason `blocked_sender`, sent back even to a node
on probation here since it carries no content. A node quarantined or blocked
here gets no bounce by that route, because this node sends it nothing and the
bounce could only pile up; it learns of a direct push's refusal from the 403.
The decision is made before the message or its sender is kept, so a refused
node cannot grow this node's trust subjects or retained events by inventing
senders. Delivered mail registers its
sender as a trust subject like any accepted event, so the receiving SysOp can
find and establish them; a node refused as a whole is already a subject from
its hello, and establishing that node is what opens its users' mail. A
recipient's own control over who may write to them is a per-user block list
(issue #817), not probation.

**The receiving SysOp sees what was refused (issue #820).** A bounce tells the
sender; until #820 nothing told the SysOp whose policy refused the letter. Every
letter addressed to this node that it refuses -- by policy on a direct push or a
relay pickup, or by a delivery bounce (no such account, a full mailbox, a
malformed or undecryptable letter) -- is kept in `link_mail_refusals`: the
sender's home node and user name, the reason code, how it arrived, when it was
first and last refused, and how many times. One row per letter (its
`content_id`), so a sender retrying by another route counts up rather than
adding rows; bounded to the 500 most recent, and 50 from any one node,
because the sender decides how many letters arrive. A direct push is refused
before `handle_events` has verified anything, and the node it names is only its
URL, so a pushed letter is recorded only once its own signature verifies
against the keys of that node, a peer this node has met
(`LinkNode.is_signed_letter_from`); otherwise anyone could put a node on the
refused list, with Establish beside it. A push that reached this node for
another node (a stale address) is not kept. The record never holds the recipient, subject, body or ciphertext: the
console's mail tools (§6.4) show senders and reasons only. The console's
refused-letter screen leads to the node's and the sender's trust screen, where
establishing or blocking happens; a refused sender whose node was refused as a
whole was never registered (above), so its node is what the SysOp acts on.

The sending node applies its own policy before anything is queued: a caller
addressing a peer this node still holds on probation is told at the To prompt
that "<node> is not linked yet; mail opens once the SysOp establishes it", and a
quarantined or blocked peer that mail to it is closed. Mail queued before a
peer lost standing waits in the outbox, expires when its work item
dead-letters, and is woken on the next sync pass once the policy allows the
peer again (§13.7), rather than after the rest of a back-off of up to six
hours.

This relaxes the earlier default, under which a probationary user's Link
messages were refused. In practice that swallowed every message between newly
linked nodes: the refusal happened before the sender was registered, so the
receiving SysOp had nobody to establish, and no bounce reached the sender.

Configuration may make these defaults stricter. Relaxing them is an explicit,
audited SysOp safety deviation. Vouches are signed, scoped, expire after at
most 180 days, and may be renewed or revoked. Revocation removes current
support but neither erases history nor accuses the subject of abuse.

A node issues a vouch only because its SysOp chose to (issue #589). The SysOp
records an intent to vouch for an identity the node has met, with a reason
that is published inside the signed object; a reconcile then keeps the node's
signed vouches in line with its standing intents. It runs on every sync pass,
and at once from the SysOp console when that console is running inside a Link
node; the offline admin console has no node identity, so there an intent waits
for Link. The reconcile signs a vouch for an intent that has none, renews one
with 30 of its 90 issued days left or signed by a key the node has since
rotated away from, and signs a revocation when the intent is withdrawn, its
reason replaced, or the identity becomes quarantined or blocked on the issuing
node: a node does not tell the network it stands behind an identity it refuses
to deal with. The intent survives such a restriction, so lifting it restores
the vouch. A renewal overlaps the vouch it replaces rather than revoking it,
which adds no weight because receivers count domains, never vouches.

An issued vouch has no effect on the issuing node's own policy. Local counting
admits a vouch only from a configured reporter, and a node is not its own
reporter; establishing an identity locally is an override. A node does not
vouch for itself or for its own users: the home node is the one party that
cannot be independent of them, and what such a statement should mean is not
decided.

### 12.5 Evidence classes and attribution

“Objective” has two classes because not every receiver measurement is
independently provable:

1. **Self-verifying protocol evidence** includes the signed objects needed to
   reproduce the violation, such as conflicting valid extensions of one head.
2. **Observer-attested protocol/resource evidence** records measurements a
   third party cannot reconstruct, such as flooding, malformed requests,
   timeouts, non-delivery, or receipt of an invalid signature.
3. **Subjective content reports** express moderation judgments. Signed content
   may be referenced, but the judgment is still opinion.

Protocol-integrity categories are `signed_equivocation`, `revoked_key_use`,
`invalid_authority`, and `invalid_signature_delivery`. Resource categories are
`malformed_traffic`, `request_flood`, `quota_evasion`, `inventory_nondelivery`,
and `relay_abuse`. Content categories are `spam`, `harassment`,
`illegal_content`, `off_topic`, and `other`. The object version may add
categories later; an unknown category may be retained for diagnostics but has
no automatic policy effect until local software/configuration understands it.

An invalid signature does not prove that its claimed signer created or sent
it. Locally it may justify action against the direct delivery peer; remotely it
remains that observer's claim about the delivery peer. Unsigned malformed
traffic is attributable only to the connection identity available at receipt.

Subjective reports affect only content conduct and can never automatically
trigger transport quarantine. Resource reports affect resource policy;
self-verifying integrity evidence affects integrity policy. UI code must not
collapse these dimensions.

### 12.6 Signed trust signals

A trust signal is an immutable signed object containing protocol/object
version, issuer, stable signal ID, canonical node/user subject, dimension,
category, evidence class, embedded evidence or digest plus locator, observation
and issuance times, expiry, optional explanation, and—when revoking—the exact
signal content ID.

On Link v1 the durable object types are `trust_signal`, `trust_revocation`,
`trust_vouch`, and `trust_vouch_revocation`. They use the ordinary canonical
Link envelope (`netbbs_protocol`, `object_type`, `payload`) plus a detached
base64 signature, but they are stored separately from `link_events` and never
enter content-event flood gossip. `issuer_fingerprint` is the stable node
fingerprint; the detached signature is made by that node's currently
authorized operational signing key. Node subjects contain `kind` and
`node_fingerprint`; user subjects additionally contain `opaque_user_id`.

Evidence has one of two exact forms. Embedded evidence is `{mode: embedded,
data: ...}`. Referenced evidence is `{mode: digest, sha256, size, locator}`.
The signed size may not exceed the evidence limit. A revocation names one exact
`revoked_content_id`, must have the same issuer as its target, and uses the
signal- or vouch-specific revocation type so an ambiguous target lookup cannot
change object-family semantics.

Revocation is a new signed object and never deletes the original. Receivers
reject invalid category/evidence combinations, expiry before issuance, and
issue times over five minutes in the future. Receipt time is retained
independently. Content IDs provide replay deduplication.

Receivers clamp active lifetimes:

- self-verifying protocol evidence: 90 days;
- observer-attested protocol/resource evidence: 7 days;
- subjective content reports: 30 days;
- vouches: 180 days.

Renewal requires a fresh signal. Expired/revoked signals leave automatic policy
but remain under bounded audit retention. Digest-only evidence (`mode: digest`)
is stored and served like any object and never counts: a node does not fetch
from a locator, so nothing could reproduce it (issue #1036). Failure to fetch is
not evidence against the subject; an issuer that wants its signal to count
embeds the proof.

A self-verifying signal counts only when this receiver reproduces its
evidence itself (issue #1036). On Link v1 the one integrity violation a
receiver can prove to itself is `signed_equivocation`: embedded evidence
`{"kind": "signed_equivocation", "objects": [a, b]}` holding two different
signed Link objects in the same slot of one append-only chain -- the same
`previous_transition_id` of one subject's key-transition chain (signed by its
root key), the same `(root_post_id, previous_event_id)` of a post's content
chain (edit, moderator edit, tombstone), or the same `(board_id,
previous_event_id)` of a board's lifecycle chain (origin transfer, closure) --
both verifying under keys this receiver itself attributes to the subject node:
its root key, or an operational signing key its chain has not called
compromised. A compromised key's signature proves nothing about who made it.
The other integrity categories (`revoked_key_use`, `invalid_authority`,
`invalid_signature_delivery`) assert what a third party cannot reconstruct, so
from a remote issuer they never verify. A signal that does not reproduce is
kept, listed as unverified, and counts toward no quarantine threshold; like
any claim, it still holds back graduation. One about a node this receiver does
not know yet is re-checked, a bounded number per sync pass, once it does.

Verified evidence counts toward the two-domain threshold; it does not become
the receiver's own local observation by itself. A proof is a proof whoever
delivers it, but independent domains still have to report it before a remote
report quarantines, so one reporter -- and, since #589, one node's automatic
issuance -- cannot quarantine a subject at every subscriber on its own. An
honestly forked node (restored from an old backup, a cloned VM) is still
quarantined once two domains have seen it, and its SysOp's remedy is the
recovery rule of §12.9. Inactive signals and evidence are retained for 365
days by default, unless an active decision or explicit legal/diagnostic hold
still references them. Later pruning preserves the content digest and decision
audit so historical enforcement remains explainable without unbounded blobs.

### 12.7 Propagation, independence, and Sybil resistance

Trust signals are not flood-gossiped with content events. A node explicitly
subscribes to selected reporters and pulls their issuer-signed signals. A
carrier may serve an unchanged signal, but the receiver verifies the issuer
and ignores unconfigured issuers.

The pull is an authenticated Link request rather than durable content. It
binds requester, responder, requested issuer, optional last-content-ID cursor,
page limit, creation time, a 128-bit nonce, and a `revocations_only` containment
flag under the requester's current
operational signing key. The responder requires a completed hello, matching
requester/responder identities, a valid signature, a five-minute freshness
window, and a bounded nonce replay cache. Responses contain only unchanged
stored objects for the requested issuer, in the order the responder stored
them, plus `more_available`. Storage order rather than receipt time, because
a revocation is always stored after the object it retires whatever the wall
clock said, and a subscriber that met them the other way round would skip the
revocation and then admit what it revoked. A node's own signed objects are
stored with the ones it has admitted, under its own fingerprint, so the same
pull serves them. A carrier gains no authority: the request
is addressed to the carrier, while every returned object's independent issuer
signature and local reporter configuration still control admission.
When a configured reporter is quarantined, ordinary subscription sync stops;
the receiver may use only `revocations_only=true`, which serves unchanged
signal/vouch revocation objects and never advances the ordinary subscription
cursor. A manually blocked reporter receives no containment exchange.

**An issuer nobody can dial (issue #627).** A pull needs an address, and an
outgoing-only node (§8.4) advertises none, which is the ordinary case for a
node behind a home connection. Such a node deposits what it signs at the
nodes that relay for it (§8.5): `POST /link/v1/trust-deposit/{fingerprint}`,
carrying the node's own objects in the order it signed them and the signed,
fresh, replay-bounded authorization the file route also uses. A relay accepts
a deposit only from a node it has agreed to relay for, which is the existing
opt-in and the existing cap on whom it holds things for; only objects the
depositor issued itself; and only those that verify under the depositor's
current key, counting the rest and leaving them out, since an issuer's store
keeps what its earlier keys signed and re-signs what still matters. Only the
issuer may deposit, although the objects would verify whoever sent them: the
order they are stored in is the order subscribers read them in, and a third
party replaying a withdrawn vouch ahead of its revocation would revive it.
The depositor keeps a position per relay, sends only what is new, and starts
over at a relay it selects afresh. Each deposit names the last object handed
over before it, kept by the relay or not, and with nothing new to send the
depositor still makes one such request a pass. A relay that does not
remember that object has lost
something, to a restored backup above all, and could otherwise be left
serving a vouch without the revocation that followed it; it answers 409 and
stores nothing, and the depositor starts over there, where what the relay
still holds keeps its place in the order. A relay that refuses for any other
reason (it does not relay for the depositor, lacks the route, or is full) is
logged, left alone for an hour, and named on the vouch screen of the
depositing node as not having taken its vouches at the last attempt.

What is deposited is *carried*, and kept apart from what the relay has
admitted. Admission is application: an object already in the admitted store
counts as replayed and is never applied, and a vouch the relay merely carries
must not look like one it counts. A relay whose own SysOp names the depositor
a reporter cannot pull from it any more than anyone else can, so in its own
sync pass, for as long as that node's own descriptor names it as a relay, it
reads what it carries exactly as a subscriber reads a carrier:
under a cursor, object by object, with the same skips and the same stall.
That works whether the depositor was named before the deposit or after it,
follows a widened grant, and leaves a rejected batch to be retried. Nothing is
admitted when the deposit arrives. Carriage is bounded per depositor at 4,000
objects and 32 MiB, refused beyond that; an object already expired is not
taken, and one that expires is dropped the next time anything is deposited.
An issuer's stream is served from exactly one store, the carried one while
it holds anything of that issuer's, because a cursor is a position in one; a
cursor from the other reads as unknown and the subscriber starts over.

A subscriber whose reporter cannot be dialed reads the relays that reporter
publishes in its own descriptor, and pulls the carrier form of the request
from one it has itself completed a hello with and can dial. It verifies what
comes back against the reporter's identity however it learned it, by hello or
by introduction (§8.11); a relay answers an identity request for a node it
relays for, and a page that stops at a key the subscriber has not learned
makes it ask that relay for a fresher bundle at once. A reporter this node has
not met, and has not blocked, is asked about at the relays it publishes that
this node has met, or failing those at up to three dialable peers, and for
a reporter already known by introduction no more than once an hour: one named
by fingerprint
alone so that it becomes a trust subject the SysOp can establish at all, and
one known by introduction so that its descriptor is refreshed, since it may
have published no relay when it was learned or have moved to another since.
That happens before probation ends the matter. None of this relaxes
what §12.4 requires of a reporter: it has to be established here before it
is pulled, which for a node never met means by override.

Remote attestations (§5.5) are not carried this way. They hold a caller's
birthdate or real name, and a carrier would either read them or enforce the
recipient list on the issuer's behalf. They travel instead as a snapshot sealed
to each recipient, through the recipient's relays (issue #632).

Distinct fingerprints do not prove independence. Automatic policy counts
locally assigned trust domains:

- reporters in one domain contribute at most that domain's weight;
- default and normal maximum domain weight is `1.0`;
- remote-signal quarantine requires two domains and total weight `>= 2.0`;
- vouch/reputation paths never multiply weight;
- reporter, domain, weight, and category changes are audited SysOp actions.

An operator may configure a jurisdictional/emergency key as sole authority for
named categories. This explicit local exception is displayed as such; weight
alone never bypasses the two-domain rule. A sole authority speaks alone but
does not skip proof: its self-verifying signal counts only once the evidence
reproduces here, like any reporter's (issue #1036).

Default trust-ingress bounds are 100 signals or 1 MiB per response, 1,000
active signals per issuer, 10 active signals per issuer/subject/category, 256
KiB embedded evidence per signal, and a separate ingestion budget in addition
to the ordinary request throttle. Over-limit input is rejected or deferred
visibly, never converted into evidence.

A subscriber distinguishes an object it cannot use from a response it must
not trust, and both from an object it cannot use *yet*. A rejection that time
or state can undo, such as an issue time in the future or a full quota,
rejects the batch, which is retried from the same cursor. An object outside
what the subscriber configured its reporter for, a revocation naming an object
the subscriber does not hold or one already revoked, and an object signed by a
key the issuer has since replaced, which is what an issuer's stream holds
after it rotates, are each skipped while the rest is admitted and the cursor
moves on. Treating those as fatal would wedge the subscription: the batch is
abandoned, the cursor stays, and every later pass meets the same object first.
A skipped object is not stored, so changing a reporter's grant resets that
reporter's cursor and the next pass re-reads its stream, where everything
already held is a replay.

An object that verifies under no key the subscriber knows for its issuer is
different. The ordinary cause is that the subscriber is the stale party: the
issuer rotated and re-signed everything, and the subscriber has not completed
a hello with it since. Skipping would move the cursor past every re-issued
vouch and every revocation. The subscriber therefore tells the two apart by
the issuer's own transition chain. A superseded key's signature is skipped for
good; an unknown key's stops the page there, with the cursor left on the last
settled object, and the next pass retries after the next hello.

A cursor the responder cannot resolve is its own case (issue #621). A
responder restored from an older backup, or recreated, will never again hold
the object a subscriber's cursor names. It answers HTTP 400 with `reason_code`
`unknown_pull_cursor`, and the subscriber forgets the cursor and re-reads that
issuer from the start, where everything it holds is a replay and the page
budget bounds the cost. This also
means a cursor saved from something a responder never stored cannot wedge a
subscription for longer than one pass. The subscriber's cursor in any case
moves only past objects it could authenticate: one signed by the issuer's
current key, or by a key the issuer has replaced. A served entry whose shape
does not allow a signature check rejects the page.

A reporter has to be established before any of this happens. Under the policy
a running node enforces, a probationary, quarantined or blocked reporter is not
pulled in the ordinary way, and a vouch from a reporter that is not established
in identity integrity and resource behavior does not count. Naming a reporter
is the grant of authority; establishing it is a separate act, by override or by
graduation.

### 12.8 Quarantine effects

Direct local self-verifying integrity evidence may quarantine immediately.
Remote signals must satisfy their configured category rule and the independence
threshold. Observer reports normally tighten limits or extend probation before
quarantine. Subjective reports may hide or moderate named user/content
projections but never quarantine transport automatically.

Node quarantine stops ordinary outbound sync; rejects ordinary events,
inventory, Link messages, files, relays, and peer introductions before new
remote state is persisted; removes the node from relay/candidate selection;
and suppresses applicable locally displayed content. Previously accepted
events, content, identity history, and audits are preserved, not deleted.

A narrow, separately rate-limited containment path may accept verified hello,
key transitions, signal revocations, and recovery metadata. It cannot carry
ordinary content or services. User quarantine affects that user, not unrelated
users, the whole home node, or content merely relayed by the node.

Manual block is harder: it denies even containment until removed. Existing
bytes still are not deleted. Rejections and suppression use stable reason codes
for protocol, diagnostics, and SysOp UI while keeping private reporters, notes,
and policy configuration undisclosed.

The Link HTTP enforcement point runs only after enough cryptographic parsing to
attribute a request, but before remotely influenced persistence or service work.
The normal runtime enables this gate for hello, events, inventory, trust pulls
and deposits, file chunks, relay consent/mailboxes, and peer introduction.
Peer-list exchange, file-chunk pulls and trust deposits use authenticated
POSTs from completed peers; Link v1
reuses an empty, signed inventory-request authorization envelope so requester,
responder, freshness, nonce replay protection, and current operational-key
verification have one existing definition rather than a second near-identical
request type. The URL fingerprint is routing information, never attribution.
Probationary inventory responses use one quarter of the established event
budget. Valid board posts from probationary users enter the local pending
approval queue, and so do their edits: an approved post must not be rewritten
with unreviewed text. Link mail (issue #804) and Linked chat lines (issue
#860) from a probationary user of a node allowed here are delivered: mail goes
to one recipient, and chat has no approval queue, so refusing a probationary
caller's line left every caller of a newly met node unheard in a Linked channel
until they graduated. Quarantine and block still refuse both. Other services
without an approval projection, such as file uploads, refuse such content with
a stable reason code.

A pushed events request is judged event by event once the sending node itself
is allowed (issue #897). An event refused for its author does not refuse the
rest: the receiver takes what it may and answers 200 with
`refused: [{content_id, reason_code}]` beside `accepted`. Only a request with
nothing acceptable left is refused outright with 403, and that body carries the
same `refused` list, so a single pushed letter keeps its refusal. A refusal
about the sending node remains a 403 for the whole request. The sender sets
each refused event aside for that peer for `DEFERRED_EVENT_RETRY_SECONDS` and
then offers it again while the peer's inventory still asks for it, so content
from an author on probation there arrives once that author may post, and
refused events never fill a request ahead of everything else. A partial
refusal is about authors, not the sending node, so it does not mark the peer
as refusing this node's content. Senders older than this rule ignore
`refused`, and a 200 means only that the rest arrived.

Enforcement attributes independently signed content to its author/home node,
not to a carrier recorded in `link_events.sender_fingerprint`. Current display
suppression is evaluated from retained signed authorship at read time; changing
or clearing local policy therefore hides or restores projections without
rewriting or deleting the accepted event bytes.

Suppression applies to every surface that shows or counts content, not only
the page that lists it (issue #677):
- A board page is filled from visible posts only. Its older/newer links count
  only visible posts, so hidden posts neither shorten a page nor produce an
  empty one.
- Post counts, `[N]ew scan` unread counts for boards and channels, and
  "replies to you" exclude hidden content.
- Local search excludes hidden posts and hidden channel messages.

Visibility is decided per event in Python rather than in SQL, but it depends
only on the event's author. A count therefore looks up each distinct author
once, not each post. Paging over a long run of hidden posts costs further
query batches rather than returning a short page.

A local tombstone stays terminal once the expiry sweep has aged it: the check
is for any tombstone revision in the chain, whatever its status.

### 12.9 Recovery, partitions, and explainability

Absence, failed dials, and partitions are never evidence. Partitions create no
reports and do not multiply old signals. Signal expiry continues on local time
so an accusation cannot become permanent through disconnection.

Automatic quarantine ends only after every trigger is cleared, expired, or
revoked and a 24-hour recovery hold passes without a fresh trigger. Recovery
returns to probationary, not established. Self-verifying equivocation or
confirmed key compromise also requires SysOp review or verified root-key
recovery; scoped resource/content restrictions may recover automatically. For
equivocation a node observed itself (issue #589), the observation expiring is
not that review: the subject stays quarantined, reason
`equivocation_review_required`, until a SysOp clears the evidence on its trust
screen, and then takes the ordinary hold.

Effective state is a persisted projection recomputed transactionally on input
changes, at startup, and on every Link sync pass. The last is for changes due to
time alone (a recovery hold's release, an override's or a signal's expiry,
probation's age requirement), which have no input change of their own; without
it a running node applied them only at its next restart (issue #802). For every restriction the SysOp can inspect the subject,
dimension, effects, rule/threshold, evidence, counted domains/weights, times,
overrides, audit history, and requirements for release. Counted domains and
weight appear whenever the dimension has a self-verifying identity report that
counts toward the two-domain threshold, whatever decided its state; a
dimension that cannot quarantine by that threshold never shows them. The
console states them as the distance to quarantine
("1 of 2 domains, weight 1.0 of 2.0"), so a SysOp sees how close one more
report would bring the subject (issue #752). Probation's
`active_trigger_count` counts every applicable dimension, because any trigger
blocks graduation in all of them; `dimension_trigger_count` beside it counts
only the dimension shown. Caller-facing behavior
states that local policy restricted content/delivery without claiming a
network-wide verdict or leaking private evidence.

### 12.10 Required validation

Phase-4 implementation must test Sybils in one domain, colluding domains below
and above threshold, compromised reporters, expiry/revocation, replay/stale/
future/oversized signals, reproducible and false evidence, invalid-signature
attribution, subjective-report isolation, partitions and recovery, restart
reconstruction, overrides, preservation without deletion, user/node scoping,
containment recovery, and real SQLite/transport resource bounds.

Public readiness additionally requires a SysOp trust/explanation surface,
manual block/quarantine/recovery workflows, and dogfood with independently
administered nodes. Unit tests alone are not a public-network claim.

### 12.11 Public-readiness checklist (issue #131)

This section is the honest statement §12.10 itself requires: what is actually
validated as of this writing, and what still is not. Update it in place as
coverage changes rather than appending a superseded status below it.

**§12.10's scenario list — validated:**

Sybils in one domain; colluding domains below and above threshold;
compromised-reporter removal (with preservation, not deletion, of the
underlying signal); expiry/revocation; replay (both at the signal level and
the request/nonce level); future-dated signals; oversized signals;
reproducible and false evidence; invalid-signature attribution; subjective
content reports never escalating to transport quarantine; restart
reconstruction; overrides (both application and clearing) as an audited,
reversible transition; preservation without deletion; user/node subject
scoping; containment recovery; and real SQLite/transport resource bounds
(per-subject/category quotas, bounded evidence fetch). All of the above are
covered by tests exercising the real policy/enforcement code paths, most of
them (Sybil weighting, replay/staleness, resource bounds) over a real
loopback transport, not only in-process function calls — see
`tests/test_link_trust.py`, `tests/test_link_trust_wire.py`, and
`tests/test_link_transport.py`.

**Known, accepted gaps in that list** (small, not believed to hide a real
policy hole, but not independently proven either):

- A trust signal that is already past its own declared expiry strictly *at
  the moment it arrives* has no dedicated test distinguishing it from
  ordinary expiry-driven exclusion — it is caught by the same general
  expiry filter every other expired signal is, but that filter's coverage
  of this exact timing has never been asserted directly.
- Trust state under a genuine network partition is validated only as two
  separate proofs, not one combined scenario: `tests/test_link_convergence.py`
  proves generic Link partition/restart convergence with no trust content
  involved, and `tests/test_link_trust.py`'s recovery-hold tests prove
  trust-state recovery timing on a single node with no partition involved.
  Nothing currently drives a real multi-node partition *of trust-affecting
  traffic specifically* through to convergence.

**Public-readiness items:**

- **SysOp trust/explanation surface** — built and reachable through the real
  Telnet menu: subjects, per-dimension state and explanation, override
  application, override clearing, and decision history are all exercised
  end to end through `admin_menu` in `tests/test_admin_flow.py`, not only
  at the `netbbs.link.trust` function level.
- **Manual block/quarantine/recovery workflows** — same real-menu coverage
  for both directions: applying a block through the UI and clearing one
  back to a recomputed state are each tested.
- **One real multi-node exercise covering configuration, quarantine,
  explanation, and recovery together** —
  `tests/test_link_transport.py::test_real_transport_enforces_probation_
  quarantine_block_explains_and_recovers` drives two real nodes over a real
  loopback HTTP connection through the full sequence: probation, an
  established override, escalation to quarantine, escalation to a manual
  block (with the previously-accepted bytes still present and the
  quarantined event still absent), reading the exact SysOp explanation
  surface for that blocked state, clearing both overrides, an explicit
  re-vouch, and a previously-refused push and hello both succeeding again
  over that same connection afterward.
- **Dogfood with independently administered nodes** — **not yet done.**
  Issue #83 tracks this and is deliberately independent in duration from
  this gate (its calendar length does not block the rest of #131), but it
  has not itself happened. This is the one item on this whole checklist
  that automated tests cannot substitute for.

**What this checklist does and does not claim:** every scenario above having
a real, passing test means the *implemented* Phase 4 model behaves
correctly against every adversarial case design doc §12 currently specifies,
including through real transport, storage, and restart. It does **not**
mean NetBBS Link is ready for public, stranger-to-stranger federation —
that additionally requires the independently-administered dogfood above,
and remains explicitly out of scope until it happens. Treat any future
claim of public readiness that does not point back to a completed dogfood
exercise as premature.

---

## 13. Runtime, persistence, and operations

### 13.1 Database execution model

Interactive and background Link work use separate single-worker database lanes,
each with its own SQLite connection and bounded submission depth.

This isolates human-paced foreground work from sustained background federation
traffic while preserving simple synchronous domain functions.

SQLite retains its normal single-writer behavior. `busy_timeout` handles short
cross-lane contention; no application-wide write mutex is introduced.

A cancelled awaiting coroutine does not abort an already-running worker-thread
operation. The database operation completes or rolls back even when the caller
no longer receives the result. Callers must account for that semantic when
performing follow-up state changes.

Shared live `LinkNode` projections are event-loop-owned. A lane-dispatched
function may build and persist events but must not mutate live Link state from a
worker thread.

### 13.2 Atomic invariants

A read-check-write invariant across connections requires one explicit write
transaction:

- begin the write transaction before reading;
- re-fetch current state;
- evaluate safety and no-op conditions from fresh rows;
- write the mutation and audit record atomically;
- roll back on every failure.

The last-usable-SysOp guard is the reference pattern.

### 13.3 Migrations

Migrations are append-only. Never edit a migration which may already have
shipped.

SQLite table rebuilds are dangerous when the rebuilt table is a foreign-key
parent: dropping it can trigger cascade or `SET NULL` actions before the
replacement exists. Prefer `ALTER TABLE ADD COLUMN`, indexes, and explicit
cleanup over rebuilds. When a rebuild is unavoidable, test it against realistic
related rows and the actual dependency graph.

A database from a newer build must fail startup clearly. A matching
`user_version` cannot prove an operator has not manually changed old schema;
manual schema mutation is unsupported unless a future schema fingerprint is
introduced.

### 13.4 Backup and restore (issue #60's first operational slice)

War Dialer coverage (issue #362): node backups also include existing configured
shared worlds as checksummed SQLite snapshots paired with the node's user-ID
namespace. Restore requires an explicit destination for every world and excludes
active game sessions. The world ownership, limits and rollback contract is recorded
in the War Dialer storage decision in ?16; manual activation is in the door guide.

Voidrunner coverage (issue #310): ordinary CLI and SysOp node backups include the
node's Voidrunner save directory (`<db>.doors/voidrunner/`, or the SysOp's
`VOIDRUNNER_SAVE_DIR`), when present, under a checksummed `voidrunner/` component.

How that directory is chosen is itself part of the contract (issue #555), because
the door and the backup CLI are different processes and need not share a `HOME`:
`examples/netbbs.rc` starts a node with `HOME` set to the state directory, while
an operator runs the backup from their own shell. The node therefore records the
directory it would hand a door, once its listeners are bound, and the CLI reads
that. Three provenances exist and are reported differently, because they carry
different amounts of confidence: an **operator-supplied** `--voidrunner-save-dir`
wins over everything; a **node-recorded** path is authoritative, so finding it
empty is a fact about the node; an unrecorded node whose `<db>.doors/voidrunner/`
exists is the same fact, derived from the database path rather than read; and an
**unrecorded** lookup with neither falls back to the calling process's own home
and is a guess, reported as one whether or not that directory happens to exist. A guess that finds files is the dangerous case -- an
operator who once ran a node from their shell has exactly that directory, holding
exactly the wrong careers -- so it is captured but never presented as a finding.
The chosen directory and its provenance are written into the manifest as captured,
not re-derived afterwards: a backup is live-safe, so the node may start midway and
a later lookup can describe a directory the archive never inspected. Capture preserves career, previous,
recovery and score JSON bytes, including damaged careers needed for repair;
temporary files and OS lock files are excluded. A bounded maintenance lease
prevents new game launches and refuses capture while an existing pilot is active.
The gate lives inside the save directory. Restore preserves that directory and
its lock inodes, switching its data entries under the lease; existing provisioned
save directories require no parent write permission for normal play or capture.
The BBS may keep running, but Voidrunner sessions must be closed. Capture precedes
the database snapshot so a newly registered user's captured career cannot refer
to a user ID newer than that snapshot. This is an offline game-data snapshot,
not a guarantee that every node artifact was written at one global instant.

Restoring a backup containing this component requires an explicit
`--voidrunner-to` destination; archive metadata never chooses a live path. The
operator must use that directory for the restored service. The component is
staged and rolled back beside its destination, allowing a different filesystem
from the database. Node and game switches share one recovery journal; failures
roll back both, and any retained external rollback location is recorded beside
the ordinary rollback generation. Journal updates use flushed atomic replacement;
a failed post-switch update also triggers rollback, retaining the last usable
journal if recovery fails. Existing backups without Voidrunner leave
external careers alone. Cross-host shared directories and simultaneously running
different game builds remain unsupported. Limits are 10,000 captured files,
4 MiB per file and 512 MiB total; unsupported entries or exceeded limits fail
clearly rather than silently producing incomplete coverage.

Door outbound receipt coverage (issue #556): a door's result receipts under
`db_path.parent / "door-outbound"` are node state and are captured as a
checksummed component with the rest of it. They were beside the database from
the start so that a backup could carry them, which is not the same as a backup
carrying them; before this component a restored node came back holding a door's
posts and none of their outcomes. Capture precedes the database snapshot, in the
direction that matters: a `"posted"` receipt is written only after its post is
committed, so every receipt in an archive names a post that archive's snapshot
contains. The reverse pairing — a post whose receipt is missing — remains
possible and is what the door contract already describes; a receipt naming a
post the restored database never issued is the one a door could act on, and this
ordering rules it out for every post the node still had. A post the node itself
deleted is the exception and is deliberately preserved: deleting a board removes
its posts and leaves the receipts, so the running node already holds that pair,
and an archive that quietly dropped those receipts would restore a node tidier
than the one it was taken from. Capture is not a validation pass over a door's
history. Only what NetBBS itself wrote is captured (a
regular file named as `_write_result` names one, within its size), bounded by
the door module's own retention rule per door and by 64 door directories
overall; anything else found there is left in place and counted in the manifest
rather than captured or called corruption. Restore replaces the live receipts in
whole with the archive's own — including replacing them with nothing, when the
archive predates this component or the node had none — because receipts from a
later generation standing beside an older database claim post IDs it never
issued. The previous generation goes to the ordinary rollback directory like
every other replaced artifact. None of this makes receipt retention a
publication ledger: it restores the same bounded, best-effort record a running
node keeps, and a door still must not infer exactly-once publication from it.

A node's recoverable state is not only its database — it is sixteen
artifacts, today scattered across derived, `db_path`-relative filenames
with no single existing tool that treats them as one recoverable set:

| Artifact | Location | Written by |
|---|---|---|
| Database | `db_path` | every domain write |
| Content blobs | `db_path.parent / f"{db_path.stem}_files"` (git-style `xx/xxxx...` sharding; excludes its own `.incoming/` staging subdirectory, which is always crash-orphan garbage — see `purge_incoming_staging`) | `netbbs.files.storage` |
| Node identity | `identity_dir` (`root.identity`, `signing.identity`, `transport.identity`, `transitions.json`) | `netbbs.link.node_identity` |
| SSH host key | `db_path.parent / f"{db_path.stem}_ssh_host_key"` | `netbbs.net.ssh.ensure_host_key`, once, at first startup. Both host keys are created owner-only (0600); a start that finds one readable by group or others restricts it to 0600 and logs a warning (issue #976) |
| SSH RSA host key | `db_path.parent / f"{db_path.stem}_ssh_host_key_rsa"` | `netbbs.net.ssh.ensure_rsa_host_key`, once, at the first startup that lacks it (issue #964). 3072 bits, offered as `rsa-sha2-512` and `rsa-sha2-256` only, never SHA-1 `ssh-rsa`, after Ed25519. It exists for clients without Ed25519 host keys, such as SyncTERM's Cryptlib-based builds |
| Managed-DNS credential | `db_path.parent / f"{db_path.stem}_managed_dns_credential"` | `netbbs.managed_dns.credential`, once, at registration (§16 Decision 7, issue #201) |
| Managed-DNS rename credentials | Previous credential plus the temporary credential-transition journal beside `db_path`; restore preserves the presence and absence of the primary, previous, and journal artifacts | `netbbs.managed_dns.credential`, during a managed-name transition |
| Welcome banner | `db_path.parent / f"{db_path.stem}_welcome_banner.ans"` | SysOp, via the welcome-banner menu screen |
| Main-menu masthead | `db_path.parent / f"{db_path.stem}_main_menu_banner.ans"` | SysOp, via the masthead menu screen (issue #161) |
| Logoff banner | `db_path.parent / f"{db_path.stem}_logoff_banner.ans"` | SysOp, via the logoff-banner menu screen (issue #177) |
| New-account banner (before signup) | `db_path.parent / f"{db_path.stem}_new_account_banner_before.ans"` | SysOp, via its own menu screen (issue #177) |
| New-account banner (after signup) | `db_path.parent / f"{db_path.stem}_new_account_banner_after.ans"` | SysOp, via its own menu screen (issue #177) |
| Board list masthead | `db_path.parent / f"{db_path.stem}_board_list_banner.ans"` | SysOp, via its own menu screen (issue #176) |
| File area masthead | `db_path.parent / f"{db_path.stem}_file_area_banner.ans"` | SysOp, via its own menu screen (issue #176) |
| Chat channel picker masthead | `db_path.parent / f"{db_path.stem}_chat_channel_picker_banner.ans"` | SysOp, via its own menu screen (issue #176) |
| Door outbound receipts | `db_path.parent / "door-outbound"` (one directory per door, bounded by that door's own retention) | `netbbs.doors.outbound`, once per answered request (issue #556) |

A backup covering only the database silently loses the SSH host key (every
client gets a MITM warning on next connect after restore) and, far more
seriously, the Link node identity (root-key custody is explicitly "part of
ordinary node backup and restore" per §4.5's node identity model, not a
separate ceremony) — so this design treats all sixteen as one atomic backup
operation, never a DB-only one.

**Mechanism**: a new `netbbs.backup` module (synchronous, path-based — no
`Database` wrapper needed, since a backup must be safely takeable against a
*live, running* node, not only an offline one) with two entry points, plus a
`python -m netbbs.backup {create,restore}` CLI in the same spirit as
`python -m netbbs.admin`. `create_backup` is also exposed through the live
SysOp `[K] Backup` screen: it uses the running node's effective database and
identity paths, chooses a fresh timestamped directory under the backup
destination -- `db_path.parent / f"{db_path.stem}_backups"` unless the SysOp
configured another directory (issue #727) -- requires confirmation, and runs
the blocking snapshot/copy work off the asyncio event loop. The CLI remains
the path-selectable, scriptable entry point. Restore remains CLI-only and
offline because it replaces the node's state.

**Scheduled backups (issue #727).** The node itself can run backups on a
schedule the SysOp sets on the Backup screen: off (the default), daily, or
weekly on one weekday, at a wall-clock time in the node's display timezone.
This is a deliberate exception to "maintenance runs only when a SysOp asks"
(the repair and GC screens), for the same reason the release check is one: a
backup nobody remembers to take is the failure it exists to prevent, and the
alternative was a cron job every SysOp had to write and keep beside the node.
The rules:

- A *slot* is one scheduled moment, handled at most once whatever its
  outcome. A failed or skipped slot (an active War Dialer world, a missing
  identity directory, a destination that has gone) is recorded in the backup
  history and not retried; the next slot is the retry.
- A node that was down across one or more slots makes exactly one catch-up
  backup when it next runs. Saving a changed schedule counts from that
  moment, so switching one on never fires for a slot already in the past.
- Retention keeps the newest N (default 7) of the schedule's *own* backups.
  Each is recorded in `scheduled_backups` when it succeeds, and only recorded
  directories that still hold a manifest are ever deleted; a manual backup,
  or anything else in the destination, is never touched. A failed deletion is
  reported and retried on the next pass.
- The destination, when set, is an existing absolute directory the node can
  write to, outside the file storage and identity trees a backup copies. It
  is never created: a destination that has disappeared (an unmounted disk)
  fails the backup rather than filling the disk beneath it.
- Scheduled runs follow the same Door installations toggle as manual ones.
- A pass runs in a worker thread with its own database handle. Shutdown does
  not cut a running backup off: the task's cancellation leaves the worker to
  finish (and logs its outcome), so the process exits once the backup is
  complete rather than leaving a half-written directory.
- The standalone `python -m netbbs.admin` edits the schedule but does not
  run it.

Copying backups off the machine, and encrypting them, remain the SysOp's.

`create_backup(*, db_path, identity_dir, destination)`:

1. **Database and managed-DNS credential generation**: reuses
   `netbbs.selfupdate.snapshot_database` verbatim
   (`sqlite3.Connection.backup()`, already proven safe against a live WAL
   database in `test_snapshot_and_restore_database_round_trip`) — written to
   `destination/<configured-database-filename>` and recorded in the manifest
   (older backups without that field retain the legacy `netbbs.db` name).
   Never a raw file copy. The primary, previous, and
   transition-journal credentials are double-collected around that snapshot,
   and the snapshot's `managed_dns_*` configuration is compared with the live
   database afterward. Any detected overlap with a credential/name transition
   retries the bounded collection, so a backup cannot pair one transition generation's
   database with another generation's bearer secrets.
2. **Content blobs**: `shutil.copytree` of the blob root into
   `destination/files/`, `.incoming/` excluded. Must run strictly *after*
   step 1, not before or concurrently — this is what makes the DB-then-
   blobs ordering below actually safe, not just a stated convention:
   `netbbs.files.entries`'s own invariant is that a `files` row is only ever
   created after its bytes are already durably written to storage, never
   the other way around. So every blob a given DB snapshot's rows could
   possibly reference was already on disk before that snapshot was even
   taken — copying blobs afterward is guaranteed to include all of them,
   plus possibly a few newer, still-unreferenced ones from uploads that
   landed in between (harmless — an orphaned blob a future GC pass could
   still reclaim, never a dangling reference). Reversing the order would
   risk the opposite, genuinely broken case: a DB snapshot referencing a
   blob the copy hadn't reached yet.
3. **Node identity, SSH host key, and every banner/masthead singleton**
   (welcome banner, main-menu masthead, logoff banner, both new-account
   banners): plain file copies (each is either static after creation or
   already rewritten via its own atomic-replace pattern — `node_identity.
   py`'s `transitions.json`, notably — so no read-tearing hazard). Every
   banner/masthead singleton is the accepted exception, with no atomicity
   guarantee on its own writes; a backup landing mid-edit could capture a
   half-written file. Accepted as-is: purely cosmetic, no correctness
   consequence, not worth an atomic-write retrofit just for backup's sake.

Writes `destination/manifest.json` last (timestamp, `netbbs.__version__`,
the database's own `PRAGMA user_version`, and a checksum per captured
artifact outside the content-addressed blob tree) — lets an operator (or a
future restore-time check) confirm what a
given backup directory actually is before trusting it. Also records
`last_backup_at`/`last_backup_path` into the live node's own `node_config`
table (same key-value store `netbbs.selfupdate`'s update-check state already
uses) — for the SysOp Backup screen and dashboard status (letter `K` for
"bacKup", since `B` is already every submenu's universal `[B]ack`), not
required for restore itself. A live-node screen can create another complete
backup; standalone `python -m netbbs.admin` remains status-only because it
does not own the running node's effective identity path.

`restore_backup(*, source, db_path, identity_dir)` reverses each of the
copies above -- **superseded by §13.10's staged/validated workflow (issue
#75)**: the
original mechanism restored each artifact in place, sequentially, with no
validation before the first live path was overwritten and no recoverable
state if interrupted partway. §13.10 replaces the restore side of this
mechanism; `create_backup` and the artifact table above are unchanged.

Restoration always resumes the same node identity; there is still no
supported way to run an old and a restored instance simultaneously -- a
second instance of the same identity already running on a *different*
machine remains an accepted, documented operator responsibility (§13.10's
own PID-file check only ever covers *this* machine).

**Explicitly deferred**: encrypting backup contents at rest (identity
material is already unencrypted-by-default on a live node — see §4.5 — and
this tool preserves whatever it finds rather than changing that policy), and
off-site/remote transport of a completed backup directory. The SysOp screen
explicitly identifies its output as local; both remain operator
responsibilities. Scheduling and retention of the schedule's own backups are
built in (issue #727, above); retention of backups a SysOp made by hand stays
theirs.

### 13.5 Bounded remote influence

Every remotely influenced queue, mailbox, retry set, retained-event collection,
transfer, relay store, and bandwidth consumer needs:

- an explicit limit;
- defined backpressure/rejection behavior;
- retry and terminal-failure policy;
- SysOp-visible state;
- safe defaults.

Security state and unread user data must not be silently discarded.

A caller's address is the key for per-source limits such as the login
throttle, so it must be one the caller cannot choose. For Telnet and SSH it is
the TCP peer. The web transport normally sits behind a reverse proxy, where the
TCP peer is the proxy for every browser caller. `[web] trusted_proxies` (issue
#980; empty by default) names the proxies, as IP addresses or networks, never
hostnames. Only for a connection from one of them does NetBBS read
`X-Forwarded-For`, and then it takes the rightmost entry that is not itself a
trusted proxy: each proxy appends the address it received the request from, so
everything left of that entry was written by the caller. A missing or malformed
entry falls back to the proxy's address. The address is decided once, when the
web session is built, so the throttle, the logs and the SysOp's screens agree.
The `Forwarded` header (RFC 7239) is not read: the proxies the Handbook
documents all write `X-Forwarded-For`.

### 13.6 Operational control surface

Issue #60 remains the authority for the incomplete production operating model,
including:

- generic persistent outbound-work items, retry, backoff, dead-letter,
  replay, and cancellation (§13.7 specifies this — `netbbs.link.work_items`,
  implemented, wired into `netbbs.link.mail`/`netbbs.link.sync`, and
  surfaced as an `[O]utbox` SysOp screen);
- sync-lag and historical/trend peer-health visibility (a read-only current-
  state view — peer count/mode, dial-reliability score, last contact (the
  last hello or events exchange with the peer itself; its descriptor
  refreshed secondhand from another node's peer list, or its mail picked
  up from a relay, does not count — issue #766), relay
  activity, board/event counters, and relay-mailbox size — is available in
  the SysOp menu's `[L]ink status` screen; per-seed health has nothing to
  show yet, since no per-seed success/failure tracking exists);
- disk, event, mailbox, relay, and bandwidth quotas (§13.9 — peer-count,
  events-per-request, carried-board-count, received-post-size, request-
  body-size, and request-rate quotas, implemented. Event-retention/purging
  and node-wide disk quota are explicitly deferred out of that slice — see
  §13.9's own reasoning);
- integrity checks and crash recovery (§13.11 — a startup `PRAGMA integrity_
  check`, plus confirming migration/incoming-upload/work-item crash safety
  already held by construction — implemented);
- bounded diagnostic log retention without content logging (§13.11 — a new
  `link_diagnostic_log` table and `[D]iagnostic log` SysOp screen, warning-
  level-and-above only, age/row-bounded — implemented);
- protocol/database upgrade and rollback compatibility (§13.11 — database
  upgrades use atomic migrations plus an operator-created pre-upgrade backup
  for rollback; `netbbs.selfupdate`'s automated snapshot/rollback orchestration
  remains unwired; the wire-protocol half, `netbbs_protocol` version-checked on
  receipt for the first time, is implemented);
- graceful drain of Link work during shutdown (§13.11 — `run_link_sync`
  finishes its current pass before stopping, including waking early from
  its own idle interval sleep rather than waiting it out, bounded by the
  existing `graceful_delay_seconds`, falling back to today's hard cancel
  only past that bound — implemented);
- disaster recovery drills exercising a restore under realistic conditions
  (§13.4 specifies the backup/restore mechanism itself — `netbbs.backup`,
  implemented; §13.10 replaces its original restore mechanism with a
  staged, validated, interruption-recoverable one and proves it against
  corrupt/truncated backups, missing components, and mid-switch
  interruption — issue #75, implemented, including a documented drill at
  `docs/NetBBS-disaster-recovery-drill.md`).

An externally operated persistent Link node should not be considered production
ready before these controls exist and have been exercised.

### 13.7 Outbound work items and retry (issue #60's second operational slice)

**Scope decision, made here rather than assumed**: this does *not* uniformly
cover every retry-shaped mechanism in the Link subsystem — only the two that
actually share the same shape. Auditing what exists today:

| Mechanism | Current behavior | Fits a work-item model? |
|---|---|---|
| Board/identity event gossip (`netbbs.link.sync`) | Every node-owned event is unconditionally re-pushed to every seed, every fixed-interval pass, forever — no attempt counter, no per-peer state at all. Safe and cheap only because the receiving side's own dedup (`link_events`) makes redundant delivery free. | **No.** There is no terminal "gave up" state that makes sense — a node's own content should be gossiped for as long as the node exists. Forcing this into a per-target attempt/backoff/dead-letter model would be inventing a failure mode (and per-peer tracking overhead) this mechanism deliberately has never needed. |
| Relay selection/consent maintenance (`_maintain_relay_selection`) | Continuously re-evaluated every pass against an evolving reliability score (`netbbs.link.reliability`), not a single item that must eventually resolve once. | **No.** This is ongoing re-optimization among many candidates, not "keep trying this one specific thing until it succeeds or we give up." It already has its own retry-like model (score-driven re-ranking); wrapping it in a second, differently-shaped abstraction would just be two competing retry policies for the same decision. |
| Link mail delivery (`mail_messages.link_delivery_status`) | Every `'pending'` row is re-pushed to its recipient every sync pass, forever, with **no cap** — the schema already reserves an unused `'expired'` status value for exactly this gap (round 93), never produced by any code path today. | **Yes.** A specific payload to a specific fingerprint that must eventually be confirmed or abandoned — the canonical case. |
| Link mail acknowledgement delivery (`link_mail_acknowledgements.sent_at IS NULL`) | Identical shape and identical gap: re-pushed every pass forever, no cap, no dead-letter. | **Yes.** Same reasoning as mail delivery. |

So `netbbs.link.work_items` is scoped to Link mail delivery and Link mail
acknowledgement delivery only — the two mechanisms that are both (a) a
specific payload addressed to a specific fingerprint, and (b) currently
missing exactly the retry/backoff/dead-letter/inspection issue #60 asks for.
Gossip and relay maintenance keep their existing, already-fit-for-purpose
models unchanged.

**A second scope narrowing, discovered while designing this**: a *work
item* resolving successfully means "the payload was successfully pushed to
the recipient's transport (or deposited at a relay)" — never "the recipient
confirmed receipt." That confirmation, for mail specifically, is a separate,
higher-level thing: `apply_link_message_accepted`/`apply_link_message_
bounced` already handle it, driven by a genuine signed event coming back,
completely unrelated to whether the push itself succeeded. Conflating the
two was a real risk in an earlier draft of this design — a work item is
**"pushed"** or **"dead_lettered"**/**"cancelled"**, never **"delivered"**;
`mail_messages.link_delivery_status` keeps its own independent
`'pending'`/`'delivered'`/`'bounced'` vocabulary, driven by accepted/bounced
events exactly as today. The one integration point is one-directional: when
a `link_mail_delivery` work item dead-letters or is cancelled (the payload
could never even be successfully pushed, or a SysOp gave up on it
manually), the caller — not `netbbs.link.work_items` itself, which stays
completely kind-agnostic — sets `mail_messages.link_delivery_status =
'expired'`, finally giving that reserved value a real producer. A
successfully **pushed** work item changes nothing on `mail_messages`: it
still waits for accepted/bounced exactly as it does today, except the sync
loop stops wastefully re-pushing bytes that already arrived once — a real
efficiency fix, not just new capability.

**Schema** (`link_work_items`, matching this project's established Link-table
conventions — `TEXT NOT NULL` ISO timestamps, a `status` CHECK-constraint
enum, a partial index on the still-pending predicate):

```sql
CREATE TABLE link_work_items (
    id                  INTEGER PRIMARY KEY,
    kind                TEXT NOT NULL,  -- 'link_mail_delivery' | 'link_mail_ack'
    reference_id        TEXT NOT NULL,  -- mail_messages.link_event_content_id, or the ack row's own id
    target_fingerprint  TEXT NOT NULL,
    status              TEXT NOT NULL
                        CHECK (status IN ('pending', 'retrying', 'pushed', 'dead_lettered', 'cancelled')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    last_attempt_at     TEXT,
    last_error          TEXT,
    resolved_at         TEXT,
    UNIQUE(kind, reference_id, target_fingerprint)
);

CREATE INDEX idx_link_work_items_due
    ON link_work_items(next_attempt_at) WHERE status IN ('pending', 'retrying');
```

`reference_id` is a pointer, not a payload copy — `netbbs.link.work_items`
never stores or looks at the actual signed event bytes; the caller (mail
delivery/ack-push code in `sync.py`) already has those via the referenced
row and is the only thing that knows how to actually attempt the push.

**State machine**: `pending` (never attempted) → `retrying` (attempted at
least once, not yet resolved) → one of `pushed` / `dead_lettered` /
`cancelled` (terminal). `enqueue_work_item` is idempotent on
`(kind, reference_id, target_fingerprint)` — creating a mail message or an
acknowledgement always enqueues its work item at that same moment, so
there's no separate "did we remember to schedule this" step to forget.

**Backoff and dead-letter thresholds** (product judgment, not derived from
anything load-bearing — adjustable later): each failed attempt schedules
the next one at `min(300s * 2^attempts, 6h)` (starting at the sync loop's
own default interval, since backing off faster than the loop even runs is
meaningless, doubling from there, capped at six hours so a long-unreachable
target still gets retried a few times a day rather than trailing off to
nothing). Dead-lettered once `attempts >= 10` **or** `now - created_at >= 5
days`, whichever comes first — the attempts cap is what actually fires in
the common case (already ~29 hours of real spacing by the tenth attempt);
the age cap is a safety net for a node that was itself offline for a
stretch and so accumulated attempts slower than wall-clock time would
suggest.

**Mechanism**: no new background loop. `netbbs.link.sync.run_link_sync`'s
existing fixed-interval loop already iterates several independently-loaded
"pending work" lists every pass (seeds, pending mail, pending
acknowledgements, relay candidates) — `_push_pending_link_mail`'s and the
acknowledgement-pushing code's own `load_pending_link_mail`/
`load_pending_link_mail_acknowledgements` calls are replaced by
`load_due_work_items(kind=...)` (status in `pending`/`retrying` *and*
`next_attempt_at <= now`), and each attempt's outcome is recorded via
`record_success`/`record_failure` instead of silently falling through to
"try again next pass regardless." Everything else about the loop — lane
dispatch, per-item try/except-log-and-continue tolerance, the fixed sleep
— is unchanged.

**SysOp surface**: `list_work_items` (filterable by status/kind) plus
`replay_work_item`/`cancel_work_item` (both audit-logged via
`record_action`, matching every other SysOp-triggered mutation in this
codebase) — a new admin-menu screen, most naturally alongside `[L]ink
status` under `System`, listing dead-lettered/retrying items with a picker
to inspect one and replay or cancel it. `replay_work_item` resets a
`dead_lettered`/`cancelled` item to `pending` with `attempts = 0` and, for
`link_mail_delivery`, is the one place the caller undoes the
`mail_messages.link_delivery_status = 'expired'` side effect back to
`'pending'` — symmetric with how dead-lettering set it.

**Explicitly deferred, not part of this slice**: retention/purge of old
`dead_lettered`/`cancelled`/`pushed` rows (this table will otherwise grow
without bound — a real gap, but a generic "how long do resolved audit-
shaped rows live" question this project doesn't have an established answer
to yet, not specific to work items); applying this abstraction to any
future work kind beyond the two named here without first checking it
actually fits the shape (see the scope-decision table above — the fit
matters more than the count of kinds). The quotas/integrity-check/
log-retention/upgrade-rollback/graceful-shutdown bullets in §13.6, separate
pieces of issue #60, were open when this slice was written and have since
been closed by §13.9/§13.11.

Implemented: `netbbs.link.work_items` (schema, backoff/dead-letter,
replay/cancel, all audit-logged); `netbbs.link.mail.compose_link_message`/
`_queue_acknowledgement` enqueue a work item in the same transaction as
the row it tracks; `netbbs.link.sync._push_pending_link_mail` now attempts
only currently-due work items instead of unconditionally resending every
pending row every pass, and already skips a push entirely once a message
has resolved through some other path (a genuine accepted/bounced event, or
an earlier dead-letter); a new `[O]utbox` SysOp screen (`System` submenu,
gated on Link being configured, same as `[L]ink status`) lists
retrying/dead-lettered items and lets a SysOp replay or cancel one.
Issue #804: an attempt this node's own trust policy stops records the
`last_error` `link policy refused target` and counts toward dead-lettering
like any failed push, so such mail expires too; each sync pass first makes
every item held that way due at once when the policy now allows its target,
without resetting its attempts or age.
Verified end to end via `tests/test_link_sync.py`'s existing real-socket
sync tests (unchanged, still passing against the refactored push loop)
plus new dedicated tests for the state machine, the mail/ack integration,
and the SysOp screen.

### 13.8 Session lockdown, drain, and shutdown

Three related but distinct SysOp `[N]ode`-menu controls over who can
connect and who stays connected, each answering a different question:

| Control | Question it answers | New logins | Already-connected sessions | Reversible | Ends the node process |
|---|---|---|---|---|---|
| `[S]hutdown` | "Take this node down" | Blocked for everyone, no bypass | Warned (staged), then all disconnected (immediately, or after an operator-chosen grace period) | Yes, while still counting down — see below | Yes, once the countdown finishes |
| `[M]aintenance mode` | "Stop admitting ordinary users for now" | Blocked for non-SysOps; a SysOp can still log in | Untouched | Yes — toggle again | No |
| `[D]rain` | "Clear ordinary users off, right now" | Unaffected (not this control's job — see the reminder below) | Non-SysOps warned (staged), then disconnected after an operator-chosen delay; SysOps (including the issuer) untouched | Yes, while still counting down | No |

`[S]hutdown`'s own sequence (round 51) already had the right order in
code — lock out new logins and warn everyone immediately, then disconnect
once the delay elapses — but its confirmation prompt's wording had drifted
out of sync with that, describing disconnect happening *before* the
lockout; fixed to match the actual, unchanged behavior.

**`[M]aintenance mode`** (`netbbs.net.maintenance.MaintenanceMode.
enable_lockdown`/`disable_lockdown`/`is_lockdown_active`) is a second,
independent flag on the same class that already holds shutdown's
`activate`/`is_active` — deliberately not the same flag, and deliberately
checked at a different point in the connection lifecycle: shutdown's gate
fires before login even begins (nothing is known yet about who's
connecting, so no bypass is possible or desired — the whole node is going
away regardless); lockdown is checked *after* credentials verify
(`netbbs.net.login_flow.run_authenticated_session`), specifically so a
SysOp can still reach the menu that turns it back off. A SysOp who logs in
while it's active sees a `(Maintenance mode is ON.)` notice appended to
their welcome line; a non-SysOp sees `LOCKDOWN_MESSAGE` and is disconnected
before ever reaching the main menu. Turning lockdown on does nothing to
sessions already connected — that is `[D]rain`'s job, a deliberately
separate action, not an implied side effect. The `[D]rain` screen's own
intro text now says so explicitly too (issue below): draining alone does
not block new non-SysOp logins, only `[M]aintenance mode` does that — a
real point of confusion in practice (Thiesi's own dogfood-testing report:
a SysOp assumed draining alone would leave the node empty afterward, when
in fact it only guarantees an empty node at the one instant the disconnect
actually fires).

**`[D]rain`** (`netbbs.net.shutdown.run_drain_sequence`) borrows
`run_shutdown_sequence`'s warn-then-disconnect shape but never touches
`maintenance`/`shutdown_event` — the node keeps running throughout.
`ActiveSessionRegistry.broadcast_to_all`/`disconnect_all` both gained an
`exclude_sysops` parameter (backed by a new `is_sysop` flag recorded on
each session's entry at `mark_authenticated` time) so a SysOp, including
whoever issued the drain, is never warned or disconnected by it — the
whole point is staying connected to keep managing the node while ordinary
users clear out for a change that needs a reconnect to take effect.

**The intended workflow** (Thiesi's own framing): turn on `[M]aintenance
mode` first — if nobody else is online, or existing sessions don't need
disturbing, that alone is enough. If someone connected already needs to be
moved along, follow up with `[D]rain`. The two are composable, not
coupled: `[D]rain` never enables lockdown itself, and enabling lockdown
never triggers a drain — each is a deliberate, separate SysOp decision,
matching how follow/membership/node-carry are kept independent elsewhere
in this design (§6.6) rather than one silently implying another.

#### 13.8.1 Scheduling, cancellation, staged reminders, and visibility (round-trip closed after real dogfood use)

A real multi-day dogfood run of the three controls above (§17's own
"run it for real, not just test it" mandate) surfaced five concrete UX
gaps, all closed together since they share one root cause and one fix:

**The bug.** `[D]rain`/`[S]hutdown` each launched their sequence as a bare
`asyncio.create_task(...)` with nothing tracking that one was already in
flight. Running the same command twice launched two independent,
uncoordinated countdowns racing each other — a second, shorter delay could
disconnect everyone while a reconnecting user still had to wait out
whatever remained of the *first*, now-orphaned countdown too, with zero
visibility into any of it.

**The fix: `netbbs.net.shutdown.SequenceScheduler`.** One instance per
node *per sequence kind* (`drain_scheduler`, `shutdown_scheduler` —
constructed in `netbbs.__main__` alongside `session_registry`/
`maintenance`, threaded the same way) tracks at most one currently
in-flight sequence: its deadline, its custom message (if any), and the
`asyncio.Task` actually running it. `schedule()` always cancels-and-
replaces any existing one first — the actual fix for the stacking bug.
Deadlines are `asyncio.get_running_loop().time()`-based (monotonic,
process-local); nothing here needs to survive a restart or be compared
across processes. A signal-triggered shutdown (SIGTERM/SIGINT) registers
with the same `shutdown_scheduler` a live SysOp session's `[S]hutdown`
command would use — one shared source of truth regardless of what
triggered it, so a connected SysOp sees accurate status either way.

**Provenance and cancellability (issue #108, revised after this
subsection originally shipped).** Each tracked sequence also records
`source` (`"sysop"`, `"sigterm"`, `"sigint"`) and `cancellable`, both
defaulting to the SysOp-created shape every pre-existing caller already
had. `netbbs.__main__._install_signal_handlers` is the only caller that
passes `cancellable=False` — a service supervisor's SIGTERM/SIGINT
outranks an in-BBS choice to keep the node running, so a connected SysOp
must never be able to cancel (or, equivalently, silently *replace*) a
shutdown that supervisor triggered; escalating to SIGKILL if NetBBS kept
running anyway would defeat the whole point of a graceful stop.
`SequenceScheduler.cancel()` — the explicit "cancel it?" action — refuses
outright for a non-cancellable sequence; `schedule()` itself stays
unconditional regardless (a second real SIGTERM must still be able to
reset an in-flight SIGTERM countdown), so `netbbs.net.admin_flow.
_shutdown_screen` is the one responsible for never reaching its own
`schedule()` call in that case either — a non-cancellable, already-
scheduled shutdown gets a status-only message and returns immediately,
never the ordinary "schedule a new one" prompts. A SysOp-created shutdown
remains fully cancellable/replaceable exactly as this subsection
originally described.

**`[L]ock & drain` ownership (issue #109).** `[M]aintenance mode` and
`[D]rain` are deliberately separate, composable primitives; `[L]ock &
drain` (added after this subsection originally shipped) just composes
them for the common case a SysOp wants both together. Its own screen
(`netbbs.net.admin_flow._lock_and_drain_screen`) must not infer whether
*it* is active from `maintenance.is_lockdown_active()` alone — that bit
is shared with the plain `[M]` toggle, so a lockdown enabled
independently would otherwise be misreported as "Lock & drain already
active," refusing to start the requested drain at all (the concrete
dogfood-adjacent bug this closes). `MaintenanceMode.enable_lockdown()`
therefore also records `source` (`"maintenance"` for the plain toggle,
`"lock_and_drain"` for the composite command — the same provenance
concept as the scheduler's own `source` above, reused rather than
inventing a parallel mechanism, exactly as the issue asked), and the
composite's own drain is tagged `source="lock_and_drain"` on
`drain_scheduler` too. The screen only ever reports itself "active," or
offers to undo anything, when it actually owns the lock
(`lockdown_source() == "lock_and_drain"`) — and even then only cancels
the drain half if it owns that too, never a drain some other, later,
independent action scheduled while its own lock was still up. Lockdown
already on for an unrelated reason is left completely untouched; the
composite command only ever adds a drain on top of it, never reclaims
or later disables it.

This one piece of state is what makes the remaining four gaps closable as
straightforward reads/writes against it, not four separate mechanisms:

**1. Explicit cancel-or-replace, not silent stacking.** Re-running
`[D]rain`/`[S]hutdown` while one is already scheduled shows its remaining
time and offers `[C]ancel it`, `[R]eplace it`, and `[B]ack` (unless it's a
non-cancellable signal-triggered shutdown — see the provenance paragraph
above, which gets a status-only message and an immediate return instead).
Cancel cancels cleanly and stops; Replace continues into the ordinary field
editor, and the resulting new schedule replaces the old one via
`schedule()`; Back leaves the existing schedule untouched (issue #282
replaced the earlier "Cancel it?" yes/no, whose "no" fell into the editor
regardless). `[S]hutdown` also gained a per-invocation delay prompt for
the first time (previously a fixed `graceful_delay_seconds` config value
with no override) — it now behaves exactly like `[D]rain`, Thiesi's own
explicit ask to close a "these two feel like different features" mental
disconnect that had never been intentional, just an artifact of the two
having been built in separate rounds. The config value is now only the
*prefill default* for the prompt, and what the SIGTERM/SIGINT signal path
still uses (no one to prompt there).

A SIGTERM shutdown ends its countdown early once nobody is connected,
checked before the first warning and about once a second after it (issue
#845). A service-manager stop or restart runs on the configured default,
not a delay anyone chose for the occasion, and with no caller left there is
nobody to wait for. A console `[S]hutdown` and the Update restart keep the
full delay the SysOp chose even if everyone, themselves included, leaves:
the SysOp may come back to cancel it.

Cancelling a *scheduled* graceful shutdown needed one real design
decision: `MaintenanceMode.activate()`'s own docstring already stated "no
way back" — true once a shutdown reaches its actual disconnect step, but
no longer true for the countdown window before that, now that the window
is cancellable. `MaintenanceMode.deactivate()` is the one narrow
exception, called only from `run_shutdown_sequence`'s own
`except asyncio.CancelledError: maintenance.deactivate(); raise` handling
around the graceful countdown — reopens new-login admission if (and only
if) the countdown itself is what got cancelled, never after
`disconnect_all()` has actually run.

**2. Staged countdown broadcasts, Unix-`shutdown`-style.** Both sequences
now broadcast on schedule, then again at 5 minutes remaining and 1 minute
remaining (only the ones the total delay actually reaches — a 30-second
drain never fabricates a "5 minutes remaining" reminder it could never
pass through), then once more immediately before disconnecting —
`netbbs.net.shutdown._run_staged_countdown`, shared by both. A custom
message, if given, is broadcast verbatim at every stage rather than
varying with the remaining-time phrase — consistent with the existing
"message replaces the default text entirely" rule (Thiesi's own wording,
unchanged from before this round), just now applied at more than one
point in time.

**3. A freshly-connecting or freshly-logged-in user is told what a
one-off broadcast alone never could.** Before this round, drain state was
purely a one-shot broadcast — a user not connected at the moment it fired
(including one reconnecting *after* an earlier drain pass already
disconnected them) had no way to know a drain was still in progress until
it silently disconnected them again. Now the first main menu after login
tells a non-SysOp, once, above its prompt, that a drain is scheduled and
roughly how long until disconnection — SysOp-exempt, since drain never
actually affects them. It is told there, with the pending chat
invitation count, and not written during login, because the menu's
redraw-in-place clear would wipe it unseen (issue #923).
Separately, `netbbs.net.maintenance.LOCKDOWN_NOTICE` (deliberately
distinct wording from `LOCKDOWN_MESSAGE`, the actual non-SysOp rejection)
is shown to *every* connecting client right after the welcome banner,
before credentials are even checked — SysOp-ness isn't known yet, so this
can't be targeted any more narrowly, and a SysOp who's about to
successfully log in anyway must not be told "please try again later." The
existing hard pre-login `MAINTENANCE_MESSAGE` (shutdown's own unconditional
gate) also gained the scheduled shutdown's own remaining time, read from
`shutdown_scheduler`, when one is available.

**4. A visual, persistent indicator — not just a one-time line easy to
scroll past or never see at all.** The real dogfood incident this closes:
a SysOp turned maintenance mode on, then forgot, and only found out when a
user reported being unable to log in. `netbbs.net.login_flow.
_draw_main_menu`'s own `Choice: ` prompt (given a live `node_controls`) now
carries a compact prefix: the current BBS time (a snapshot at draw time,
deliberately not a ticking live clock — this codebase has no per-session
background refresh mechanism, and building one just for a clock would be
disproportionate to what was actually asked for), plus at most one alert
tag, most urgent first — `[SHUTDOWN M:SS]`, else `[DRAINING M:SS]`, else
(SysOps only, by construction: a non-SysOp who reached the menu at all
already implies lockdown isn't blocking them) `[MAINT MODE]`. The `[N]ode`
admin menu itself also gained an unconditional status line (maintenance
on/off, plus either scheduled control's own remaining time) — the SysOp's
own dashboard for the exact "did I leave this on" question that prompted
the whole fix. `netbbs.rendering.ALERT_COLOR` (a new palette entry) marks
all of this consistently, distinct from `PRIVILEGE_COLOR` (an account's
own permanent access badge) and `MUTED_COLOR` (routine informational
text) — this specifically means "something time-sensitive is happening to
the node itself."

**5. Operator-visible in stdout/the process log, not only the DB-backed
moderation log.** Scheduling or cancelling a drain/shutdown, and toggling
maintenance mode, now each log one `INFO`-level line
(`netbbs.net.admin_flow`'s own `_logger`) alongside the existing
`record_action` audit row — an operator watching a foreground terminal or
`journalctl` sees it happen without a separate DB query. `netbbs.__main__.
run()` also gained one `"NetBBS is ready to accept connections"` line,
logged once every configured listener/background task has actually
started successfully (immediately before the process blocks on
`shutdown_event.wait()`) — previously each transport only logged its own
"listening on..." line individually, with no single line marking that
startup as a whole had actually finished.

### 13.9 Quotas: closing the remaining bounded-remote-influence gaps (issue #60's third operational slice)

**Audit before design, same discipline §13.7 used for work items.** §13.5
already states the general rule (every remotely influenced resource needs an
explicit limit, defined rejection behavior, and SysOp visibility); this
section is the concrete audit of where that rule is and isn't met yet, and
the scope decision for what this slice actually closes.

**Already bounded, no change needed here** — peer-list entries per request
(`_MAX_PEER_LIST_ENTRIES_PER_REQUEST = 100`), unverified candidate
descriptors (`_MAX_CANDIDATE_DESCRIPTORS = 500`, new-fingerprint admission
capped but refreshing an already-tracked candidate is always allowed),
relay-serving slots (`max_relay_clients`, decline-not-error), relay mailbox
envelopes per recipient (`MAX_MAILBOX_ENVELOPES_PER_RECIPIENT = 50`, HTTP 507
on overflow, no eviction; since issue #891 each envelope is also dropped after
`RELAY_MAILBOX_RETENTION_DAYS`, §8.5), Link mail delivery/acknowledgement retry
(§13.7's backoff-then-dead-letter), local mailbox size (`MAX_MAIL_PER_
RECIPIENT`, evict-oldest-read/refuse-if-all-unread — already applied to
incoming Link mail too, bouncing rather than silently dropping), and Zmodem
upload size (`max_upload_bytes`, checked against both claimed and actual
running size).

**Gaps this slice closes** — every currently-uncapped *admission* point in
the Link protocol, plus the two content-validation gaps that most directly
let one peer impose unbounded cost on another node:

| Gap | Fix | Enforcement idiom |
|---|---|---|
| `LinkNode.peers`/`link_peers` — any node that completes a hello becomes a permanent peer, no cap (mirror-image gap to candidate descriptors, which *are* capped) | New `LinkConfig.max_peers` (default 1000 — generous relative to §14's declared small-network scale, but no longer infinite) | Same shape as candidate descriptors: admitting a genuinely *new* fingerprint past the cap is refused; a hello from an *already-known* peer (key rotation, descriptor refresh) is always accepted regardless of the count. Implemented as `handle_hello`'s own optional `max_peers` keyword (`None` default, unbounded, preserving every prior caller) rather than `handle_relay_consent_request`'s "caller decides" split — that idiom exists specifically for relay-consent's in-band `accepted=False` reply shape, which `handle_hello` has no equivalent of; refusing a hello is a whole-request failure either way, the same shape peer-list's own internal cap already uses. Only threaded into the *inbound* path (`LinkServer._handle_hello`/`_handle_relay_mailbox_pickup`) — `dial_hello` (outbound) is left unbounded by this cap, already indirectly bounded by `_MAX_CANDIDATE_DESCRIPTORS` plus the operator's own small configured seed list. |
| `handle_events` batch size — no per-request cap, unlike peer-list's own `_MAX_PEER_LIST_ENTRIES_PER_REQUEST` from the same design round | New `_MAX_EVENTS_PER_REQUEST = 200` beside the existing constant in `protocol.py` | Reject the whole batch with `LinkProtocolError`, identical to the peer-list precedent — a genuine sync backlog still drains over several passes rather than one unbounded request. |
| `board_post`/`board_post_edit` content size — zero validation on receive, unlike locally created posts (`netbbs.boards.posts.MAX_SUBJECT_BYTES`/`MAX_BODY_BYTES`) | Apply the same two constants inside `handle_events`'s `board_post`/`board_post_edit` branches | `LinkProtocolError`, matching every other malformed-event rejection already in that method. |
| Carried-board count — `materialize_carried_board` turns any verified `board_genesis` into a local `Board` row unconditionally | New `LinkConfig.max_carried_boards` | The `board_genesis` event is still verified, accepted, and gossiped on past this node (dedup and chain integrity for *other* nodes must not depend on this node's own local storage choices) — only *materializing* a local, browsable `Board` row is refused once the cap is hit. Past the cap the resource is recorded as *offered* for the SysOp to accept, not declined (issue #561, §9.3). |
| Link HTTP request body size — neither `LinkServer` nor `WebServer` sets `client_max_size`, so both silently inherit aiohttp's implicit 1 MiB default | Set `client_max_size` explicitly on `LinkServer`'s `web.Application()`, sized to comfortably fit `_MAX_EVENTS_PER_REQUEST` worth of events (2 MiB) | Turns an accidental library default into a deliberate, documented value; aiohttp's own 413 response is unchanged (not worth reshaping into a `LinkProtocolError` payload for a request that was rejected before any handler ran). |
| Link HTTP request rate — no throttling on any Link route at all, including the two unauthenticated ones (`/hello`, `/peers`) | New `netbbs.net.throttle.LinkRequestThrottle` (a small public wrapper around the existing `_KeyedTokenBuckets` machinery `LoginThrottle` already uses internally), keyed by source address, applied via an aiohttp middleware on every route -- constructed once in `netbbs.__main__` from three new flat `LinkConfig` fields (`request_rate_capacity`/`request_rate_refill_per_minute`/`request_rate_max_tracked_sources`, not a nested sub-dataclass) and passed into `LinkServer`, the same "build once, node-lifetime, threaded into the one real server" shape `_build_throttle` already uses for `LoginThrottle` | Exceeding it returns a plain HTTP 429, no signed payload needed (an unauthenticated-route response can't be signed meaningfully anyway). `None` throttle (every caller predating this) is a middleware no-op, not a hard requirement. |

**Explicitly deferred, not part of this slice** — following §13.7's own
precedent of naming what's excluded and why, rather than silently narrowing
scope:

- **`link_events` retention/purging.** The two places this gap already exists
  in code (`storage/migrations.py`, `link/store.py`) both name the same
  blocker themselves: purging has to reckon with `handle_events`'s own
  chain-idempotency self-heal logic for `key_transition`/`board_post_edit`/
  lifecycle events first, or a purge could resurrect a fork-detection false
  positive for an event this node legitimately already integrated. That is
  its own design pass, not a quota default.
- **Node-wide disk quota on blob storage** — the literal "disk" word in issue
  #60's own bullet, and still the single largest true gap (no `shutil.disk_
  usage`, no running byte counter, no code anywhere aware of aggregate
  storage consumption). Needs genuinely new disk-usage-tracking machinery
  this codebase doesn't have yet, not a threshold check against an existing
  number — sized as its own future slice rather than folded in here.
- **`link_work_items` terminal-row retention.** Locally driven growth (a
  node's own composed mail history), not remote-influenced admission — pairs
  more naturally with issue #60's separate "log retention" bullet than with
  quotas, and every individual item is already bounded by dead-lettering.
- **Zmodem transfer-rate (time) limiting.** The existing byte-size cap plus
  `ThrottleConfig`'s connection/session limits already bound the worst case
  tolerably; true transfer-rate instrumentation isn't worth building until a
  real problem is observed.

**SysOp visibility.** `[L]ink status` gains a peer-count line showing
`current/max_peers` (matching the existing `relaying_for`-slots-in-use
display precedent) and `current/max_carried_*` lines for boards, channels
and file areas, with offered and excluded counts (issue #683);
rate-limit rejections are logged the same way `LoginThrottle` rejections
already are, not surfaced as a separate screen in this slice.

Implemented.

### 13.10 Staged, validated restore (issue #75)

**Problem, confirmed by reading the code, not assumed from the issue alone**:
the original `restore_backup` copies each of the five artifacts (§13.4)
straight into its live path, sequentially, in place -- `shutil.copy2`/
`copytree` directly onto `db_path`/`identity_dir`/etc., with the previous
live directory `shutil.rmtree`d immediately before the replacement copy
starts. Nothing about the backup is checked before the first live path is
touched (the manifest's own fields today are metadata only -- no
checksums), and an interruption mid-copy leaves neither the old state (already
half-deleted) nor a complete new one -- exactly the "restore intended to
recover a node can make it less recoverable" failure the issue describes.
The write-lock probe (`_require_not_in_use`) also only ever catches a
transaction genuinely in flight at that instant, not an idle-but-running
node holding no lock between transactions -- its own docstring already
said so.

**Mechanism**: validate everything first, stage a full copy, then switch
staged artifacts into their live paths with atomic renames -- never restore
by copying directly onto a live path again.

**1. Manifest gains per-artifact checksums.** `create_backup` now writes
`manifest["checksums"] = {relative_path: sha256_hex}` for every file it
captures *outside* the content-addressed blob tree: the database snapshot,
each of the four identity files, the SSH host key, and the welcome banner.
The blob tree needs no manifest entry at all -- `netbbs.files.storage`
already lays every blob out at `root/{sha256[:2]}/{sha256}`, so a blob's own
path *is* its claimed hash; restore verifies the tree by recomputing each
blob's hash and checking it against its own filename, catching truncation/
corruption with no extra bookkeeping and no manifest growth as a node's
file area grows.

**2. Full validation before any live path is touched.** A new
`_validate_backup_source(source) -> Manifest`, called first, unconditionally:
manifest exists and parses; every checksummed file listed is present and its
hash matches; the database snapshot passes `PRAGMA integrity_check` *and*
opens cleanly as a real `netbbs.storage.database.Database` (this reuses,
rather than reimplements, that class's own existing "refuse a schema newer
than this build supports" guard from `_apply_migrations` -- restoring a
backup taken by a newer NetBBS version onto an older install was already a
real risk this makes checked, not just checked-if-someone-remembers); the
identity directory, if present, actually loads via `netbbs.link.node_
identity.NodeIdentity.load` (a genuine functional check -- chain-to-key
consistency and all -- not just "the files exist"). Any failure raises
`BackupError` with the specific problem before a single live byte moves.

**3. Node-liveness check gains a PID file, kept as a second layer alongside
the existing lock probe, not a replacement for it.** `netbbs.__main__`
writes its own PID to `db_path.parent / f"{db_path.stem}.pid"` once
started, removed in the same `finally` that already closes the database on
every exit path (SIGTERM, SIGINT, and startup failure alike). Restore reads
this file if present and checks the PID is still alive with a portable,
best-effort liveness check (`os.kill(pid, 0)` on POSIX; a `tasklist` shell-
out on Windows for local dev/test convenience -- the deployment target is
NetBSD, where the POSIX path is what actually matters) -- refuses if alive,
catching the idle-but-running case the lock probe alone could not. A PID
file present but pointing at a dead process is treated as a stale leftover
from an unclean exit (warn, proceed) rather than a hard refusal -- the same
"an operator responsibility, not a load-bearing distributed lock" framing
this section already applies to the cross-machine case.

**4. Stage before touching anything live.** Every artifact is copied (with
its checksum reverified against the fresh copy, catching corruption
introduced by the staging copy itself, not just the original backup) into
`db_path.parent / f".netbbs-restore-staging-{token}"` -- a sibling directory
on the same filesystem as the live targets, which is what makes step 5's
renames atomic rather than a second copy.

**5. Switch via rename, not copy, with a non-silent marker for the gap
between the first and last rename.** Before switching anything, restore
writes a small state file (`db_path.parent / ".netbbs-restore-state.json"`)
naming the staging directory, a per-token rollback directory, and which
artifacts remain to switch. For each of the five targets in turn: rename
the current live artifact (if any) into `db_path.parent /
f".netbbs-restore-rollback-{token}"` under its own name, then rename the
staged artifact into the live path, updating the state file after each
completed step. If any single rename fails, everything already switched is
renamed back from the rollback directory (best-effort, since the renames
already succeeded once and are switching back onto paths that still exist)
before re-raising -- recovering the previous generation automatically in
the common case. The state file is removed only once every artifact has
switched (success) or every switched artifact has been rolled back
(recovered failure); if the process is killed outright mid-switch rather
than raising a catchable exception, the state file survives as the "clearly
identified... not a silent mixture" record the acceptance criteria asks
for, and a subsequent `restore` invocation refuses to start a new one over
an unresolved marker rather than compounding the mess.

**6. The rollback generation is not auto-deleted on success.** A completed
restore leaves `.netbbs-restore-rollback-{token}` on disk holding the
*previous* live state, not silently discarded -- matching this project's
"never silently discard state a human might still need" stance (§13.5).
The CLI prints its path; cleanup is an
explicit operator/cron action (retention/rotation stays out of scope here,
same as §13.4's own already-deferred list), not automatic.

**Disaster-recovery drill.** Documented at `docs/NetBBS-disaster-recovery-
drill.md`: stop the node; corrupt or truncate a real backup and confirm
restore refuses before touching anything live; interrupt the restore
process mid-switch and confirm the previous generation is intact or the
state file clearly names what to do; complete a real restore and confirm
identity continuity (same fingerprint), every configured transport still
authenticates, previously created local content is still browsable, and
Link resumes gossiping with its peers on restart. Proven functionally,
live, against a real running/killed/restarted node process during
implementation -- corruption refusal (checksum mismatch caught before any
live byte moved, confirmed via before/after hashing), the PID-file check
against a genuinely running process, a stale PID file from a hard-killed
process correctly tolerated rather than blocking restore, and a full
restore-then-restart cycle with identity/content verified. Running the
drill specifically on NetBSD hardware remains the one piece this design
enables but does not itself execute from a non-NetBSD development
environment.

Implemented.

### 13.10a Operating policy is tunable from the console (issue #730)

The Link policy settings (carry caps, peering, relay capacity, catalogue and
transfer limits, request rate, diagnostic retention, live-relay bounds), the
login throttle and the shutdown delays are operating policy, not bootstrap
plumbing. A SysOp tunes them over time and must not need a shell for it. Each
resolves per key: an explicit config-file or command-line value, then a value
saved from **Settings → Network & login limits**, then the built-in default.
This is the precedence `[link] enabled` already has over the participation
answer. A config file that sets nothing behaves exactly as before, and one that
sets a key keeps it: the console shows that key as set in config and does not
offer it.

Resolution happens once, at startup. None of these values is read live -- the
caps, rates and retention are handed by value to objects built at startup -- so
a saved change applies at the next start, and the console says so and shows the
running value until then. Making individual keys live is a later, per-key
change, not a promise of this design. A stored value that no longer validates
is skipped with a warning rather than keeping the node from starting.

Bind addresses and ports, `public_url`, advertised addresses, paths and
`[managed_dns]` stay config-only: a wrong listener set from inside the BBS can
lock the SysOp out of the session they would need to fix it, paths are needed
before the database opens, and the managed-DNS values are service-bound and
secret.

### 13.11 Closing issue #60: integrity, diagnostics, protocol compatibility, graceful Link drain

Four remaining, previously-open bullets from §13.6 — audited individually
below, same discipline §13.7/§13.9 already used, each narrower in places
than its one-line issue wording once actually read against the code.

**1. Startup integrity check and crash recovery.** Confirmed by grep: `PRAGMA
integrity_check` exists nowhere in this codebase except inside issue #75's
own backup-validation path — an ordinary node startup opens the database
with no corruption check at all, so a corrupted file (disk failure, an
interrupted non-WAL filesystem operation, bit rot) surfaces only later, the
first time some unlucky query happens to touch the damaged page, as a raw,
confusing `sqlite3.DatabaseError` rather than a clear diagnosis at the one
point an operator can still act on it before real damage compounds.
`netbbs.__main__.run()` now runs `PRAGMA integrity_check` immediately after
`Database(config.db_path)` opens, wrapped into the same `StartupError`
message shape that already handles "wrong build/version" — refusing to
serve traffic against known corruption, matching round 56's own "refuses to
start with zero SysOps" precedent for a startup condition worth failing
loudly on rather than limping past. **Deliberately not folded into
`Database.__init__` itself** — a full-database scan on *every* `Database()`
construction would tax every admin script and the entire test suite (2500+
constructions) for a check only the one long-lived node process actually
needs once, at its own startup; `netbbs.__main__` calls it explicitly, once,
itself.

Crash recovery beyond that single check turns out to already exist,
confirmed by reading rather than assumed: `_apply_migrations` commits a
migration's schema change and its `user_version` bump in the *same*
transaction, so a crash mid-migration simply leaves `user_version`
unadvanced — the next startup resumes migrating from the correct point,
never re-applies a partial migration, never needs new code. `purge_incoming_
staging` already treats every leftover `.incoming` file as crash debris from
a previous run and removes it before any listener starts. `netbbs.link.
work_items` are DB-row-backed with their own retry/backoff already, so a
crash mid-processing just leaves an item `pending`/`retrying`, picked up
normally next pass. This slice adds one regression test proving the
migration-crash-safety claim directly (kill a `Database()` open partway
through applying migrations, confirm a fresh open resumes and completes
correctly) rather than leaving it as an untested assertion.

**2. Bounded Link diagnostic log, metadata only.** No Link operational log
exists today beyond whatever `logging.basicConfig(level=logging.INFO)`
sends to stderr — ephemeral, unbounded (retention is entirely the process
supervisor's problem), and gone the moment a terminal's scrollback rotates
or the service manager's own log rotation fires. A SysOp investigating "why
did sync with peer X stop working three days ago" has nothing durable to
look at. Deliberately **not** a general application-logging overhaul — the
existing `moderation_log` table is already this project's precedent for a
structured, DB-backed log, and the new one is explicitly its bounded,
non-permanent counterpart: a `link_diagnostic_log` table (`id`, `level`,
`logger_name`, `message`, `created_at`), populated by a small `logging.
Handler` subclass attached to the `netbbs.link` logger namespace at startup
(catching every existing `_logger.warning`/`.error` call already scattered
across `netbbs.link.sync`/`.transport`/`.reliable_nodes` via ordinary logger
propagation — no per-call-site instrumentation needed) at `WARNING` level
and above only; routine `INFO`-level chatter stays stderr-only, ephemeral,
exactly as today. Audited every existing call site this handler will now
capture (§13.9's own audit-before-design habit, applied here to *existing*
log statements rather than a new feature): every one is already about
protocol/dial/sync *events* — a URL, a fingerprint, an exception message —
never a Link message's decrypted body, a board post's content, or any other
user-authored payload. "Metadata only, never content" is therefore a
property of which fourteen call sites happen to exist today, not a new
filter this handler has to enforce — worth re-checking whenever a future
Link module adds a new `_logger` call inside this namespace.

Both `LinkConfig.diagnostic_log_max_age_days` (default 30) and
`diagnostic_log_max_rows` (default 5,000) bound it — the handler prunes
against both on every write, cheap at this log's realistic warning-only
volume. Browsable via a new `[D]iagnostic log` SysOp screen under `[S]ystem`
(alongside `[L]ink status`/`[O]utbox`/`[R]epair carried posts`, same
`link_context is not None`-gated visibility), the same paginated-picker
shape `[O]utbox` already uses.

**3. Link wire-protocol version compatibility.** A real, confirmed gap, not
a hypothetical: every canonical event envelope already carries `netbbs_
protocol` (`build_envelope`, `NETBBS_PROTOCOL_VERSION = 1`, round 27) — but
grep confirms nothing anywhere ever reads it back on receipt. A future
protocol revision bumping this field would today be silently ignored by
`handle_hello`/`handle_events`, which would then either crash on an
unfamiliar payload shape with a confusing low-level error, or — worse —
successfully parse a subset of fields that happen to still match and
silently misinterpret the rest. `netbbs.link.protocol` gains one shared
check, applied once per envelope at the single point `handle_events`
already extracts `object_type` before dispatch (covering all nine event
types from one call site, not nine), and separately against the hello
bundle's own embedded transitions/descriptor envelopes in `handle_hello` —
rejecting a `netbbs_protocol` that doesn't exactly equal this build's own
`NETBBS_PROTOCOL_VERSION` with a clear `LinkProtocolError` naming both
versions, never a raw parse failure. "Exactly equal," not a supported range
— there is no forward/backward-compatibility promise to honor yet, since
version 1 is the only version that has ever existed; the point of this
slice is having a real, tested gate *before* a version 2 ever needs one, not
guessing at compatibility rules for a wire change nobody has designed.

The database and protocol halves use separate compatibility mechanisms.
Database migrations are atomic, a newer-than-supported schema is rejected at
startup, and the supported operator procedure requires a complete pre-upgrade
backup so code and schema can be restored together. An install from the
console (§6.7) takes that backup itself; automated database rollback is not
implemented.
The wire-protocol check above is independently implemented in
`netbbs.link.protocol`, where received envelopes are version-gated without
coupling Link compatibility to release installation.

**4. Graceful drain of Link work during shutdown.** `run_link_sync` accepts
an optional `stop_event: asyncio.Event | None`, checked once at the top of
the outer loop (before starting a new pass, not mid-pass — deliberately
simple: passes are normally sub-second, so the value of checking more
granularly inside one is marginal against the complexity of doing so) so a
currently in-flight pass, including whatever HTTP call it's in the middle
of, finishes naturally rather than being aborted mid-request against
whatever peer is on the other end — the one asymmetry ordinary user-session
shutdown didn't have, since that path already warns and waits before
disconnecting anyone. Shutdown sets the event, then `asyncio.wait_for`s the
task against `ShutdownConfig.background_task_drain_seconds` (5s default,
a dedicated timer — not `graceful_delay_seconds`, which answers a
different question, "how long does a *human* get to notice a shutdown
warning before disconnection," and is already fully spent by the time
teardown starts), falling back to a hard `.cancel()` only if that bound is
exceeded (a pass stuck on an unreachable seed's own connect timeout, say).

The loop's own trailing `await asyncio.sleep(interval_seconds)` is
interruptible the same way: `stop_event`-provided callers wait on
`stop_event.wait()` bounded by `asyncio.wait_for(..., timeout=
interval_seconds)` in place of the plain sleep, waking immediately once
shutdown signals rather than waiting out however much of
`sync_interval_seconds` (default 300s) remains — an idle sleep has no
in-flight work to protect, so cutting it short costs nothing the way
interrupting a live HTTP call would. Callers that don't pass a
`stop_event` (`None`, the default) still get the original unconditional
`asyncio.sleep`, unchanged.

`daybreak_task`/`update_check_task`/`reliable_nodes_refresh_task` get a narrower
treatment: none of them talks to a Link peer (`reliable_nodes_refresh_task` fetches
the project's own reliable-nodes roster from `www.netbbs.org`, not a peer's
Link endpoint, and already retries on its own forgiving 24h cadence), so
*graceful* draining — letting current work finish rather than aborting
it — would be solving a problem none of them actually has; all three are
still cancelled immediately, exactly as before. But `update_check_task`/
`reliable_nodes_refresh_task` both reach a blocking `urllib.request.urlopen` call via
`asyncio.to_thread`, and cancelling the *awaiting* coroutine does not stop
that underlying worker thread, which keeps running the blocking call to
completion regardless (Python cannot forcibly abort a thread) — the
shutdown teardown step that then directly `await`s the cancelled task
needs its own ceiling, or an unresponsive fetch there hangs shutdown
independent of anything `link_sync_task` does. All three now share the
same `background_task_drain_seconds` bound `link_sync_task` uses above,
applied to the cancellation-*await* rather than to "let it finish
first" — `daybreak_task` never needed this in practice (a bare
`asyncio.sleep` always cancels promptly), but gets the same treatment for
consistency, at zero real cost.

**Known residual gap (Codex review, PR #228):** the bound above only
covers `netbbs.__main__.run`'s own await of the task. The worker thread
itself is not owned by asyncio and cannot be cancelled — verified by
direct repro, it keeps running (bounded by `urlopen`'s own `timeout=30`,
so tens of seconds, not indefinitely) even after teardown gives up
waiting on it, and Python's interpreter-exit machinery
(`concurrent.futures.thread`'s `atexit` hook) still joins every
outstanding `ThreadPoolExecutor` worker before the process actually
exits, regardless of this bound. A shutdown that looks prompt in the logs
is not yet a guarantee the OS process has actually exited. See
`docs/NetBBS-worklog.md` for the fuller mechanism and candidate fixes;
unresolved as of this writing.

**5. Lingering transport connections at listener stop.** A second real
~9-minute Ctrl+C hang on ReLink (2026-09-04), distinct from the one above:
a caller's client had vanished without a TCP FIN (asleep laptop, dropped
Wi-Fi). Ending its session only exits the SSH *channel*
(`SSHSession.close`); the connection underneath stays up until the client
disconnects, which a dead peer never does. `SSHAcceptor.close()` (and
asyncio's `Server.close()` for Telnet) only stops accepting, and on Python
3.12+ `wait_closed()` blocks until every admitted connection has actually
dropped -- so teardown's listener-stop step sat for the kernel's whole TCP
retransmission timeout (`[Errno 60] Operation timed out` on macOS, about
nine minutes; longer on Linux). Both listeners now track every connection
they admit -- authenticated or not, since a connection still in its auth
handshake or Telnet option negotiation never reaches the session registry
-- wait `background_task_drain_seconds` for them to close on their own,
then `abort()` whatever is left (no disconnect handshake, which a dead
peer could never complete anyway). `TelnetSession.close` bounds its own
`wait_closed()` the same way, because `StreamWriter.close()` flushes
buffered output first and shutdown gathers every session's close.
Rejected: closing the SSH connection from `SSHSession.close` -- a live
client exits on channel close by itself, so the abort belongs at the one
place that knows every connection, and only after the normal path has had
its chance.

The same class of dead peer also holds a node slot during normal operation
until that kernel timeout, since asyncssh sends no keepalives unless asked.
The SSH listener now enables transport keepalives (30s interval, three
misses), so a vanished client is detected within about ninety seconds
instead. Deliberately not an operator setting: an idle live client answers
keepalives for free and NAT mappings benefit from the traffic, so there is
nothing a SysOp would tune. Telnet has no protocol-level equivalent; its
dead peers surface on the next write, and shutdown no longer waits on them.

**Closes issue #60.** Every acceptance criterion that issue names is now
either implemented (this slice; §13.4/§13.7/§13.9/§13.10 before it) or an
explicitly deferred, separately-tracked follow-up with its own stated
reasoning (node-wide disk quota and event-retention/purging, §13.9;
per-seed historical/trend health visibility, §13.6) — not a silently
abandoned acceptance criterion.

Implemented.

---

## 14. Testing and interoperability requirements

### 14.1 Deterministic distributed testing

Every implemented Link event family must be exercised through independent node
instances and serialization under applicable scenarios:

- duplicate delivery;
- reordering;
- dropped messages;
- partition and healing;
- restart and state reconstruction;
- malformed or forged events;
- key rotation/revocation;
- convergence after valid resends.

The harness grows with real event families. A generic harness which cannot drive
the real protocol is not sufficient.

**Cross-subsystem end-to-end scenarios (issue #80).** The deterministic
harness above proves protocol/verification logic; it does not, by
itself, prove that a caller-visible guarantee survives the seam between
subsystems (protocol verification, persistence, transport, local-domain
materialization, outbound work tracking, user-visible state). Issue #69
was exactly that: individually correct subsystems, but a self-composed
Link message was never registered where its acknowledgement needed to
find it. `tests/test_link_end_to_end.py` is the named home for this
class of test: a complete real-transport (real `LinkServer`, real
SQLite, real node identities), real-domain-read-path (an ordinary
inbox/board read, not a raw row or `known_event_ids` check) vertical
slice per currently implemented Link product surface — linked boards,
Link mail, (issue #87) linked channels, and (issue #89) remote file
catalogue/chunk transfer — each covering restart-between-stages and
duplicate-delivery. A future Link vertical slice is not complete until it
adds or extends a scenario in that file, the same way it is not complete
without unit tests for its own protocol logic. Tier-2 message routing is
deliberately not in that list — see §10.6 for why it remains deferred
rather than an active future slice.

### 14.2 Real boundaries

Use real:

- SQLite files and independent connections for concurrency and migration tests;
- sockets for transport adapters;
- serialization between separate protocol objects;
- reconstructed objects after restart;
- bounded readiness polling instead of arbitrary sleeps.

Mocks may isolate failures but do not prove the boundary being claimed.

### 14.3 Prove regression tests

When practical, demonstrate that a new regression test fails without the fix.
A test which passes both before and after the supposed fix has not proved the
bug.

Scripted terminal tests must fail fast on input exhaustion and confirm they
reached the intended path after menu or signature changes.

### 14.4 External validation

Automated tests cannot prove visual behavior or third-party interoperability.
Before calling affected functionality production ready, test as applicable with:

- a real OpenSSH client;
- real Telnet terminals;
- SyncTERM/lrzsz or another external Zmodem implementation;
- a real browser/xterm.js session;
- resize, color, CP437 art, editor, bell, and echo behavior;
- long-running operation across midnight and DST changes;
- update, restart, backup, and restore on NetBSD.

### 14.5 Canonical format compatibility vectors

Any change to the canonicalization rule (§7.2) must update
`tests/fixtures/link_canonical_vectors.json` and keep
`tests/test_link_canonical_vectors.py` passing. A vector's canonical bytes or
content ID may only change alongside a deliberate, documented
canonicalization change — never as the side effect of an unrelated
refactor.

---

## 15. Roadmap and phase boundaries

### Phase 1 — Foundation — complete

- modular runtime and SQLite storage;
- node/user identity foundations;
- password and keypair login;
- Telnet, SSH, and web transports;
- ANSI rendering and input plumbing;
- level/permission foundations;
- local boards, file areas, and chat;
- local blocklist foundation.

### Phase 2 — Complete standalone BBS — complete

- local moderation and approval workflows;
- maintenance/expiry;
- user directory, profiles, and finger-style lookup;
- channel visibility, invitations, membership, and moderation;
- local private chat, presence, aliases, and completion;
- SysOp administration and node controls;
- TUI/screen-buffer foundations;
- ANSI and prose editors.

### Post-Phase-2 local additions — substantially complete

- local Communities and Community-scoped authority;
- identity attestation and gates;
- local asynchronous mail;
- self-update foundations and scheduled checks;
- registration-mode and account-lifecycle refinements.

### Phase 3 — Link connectivity and asynchronous services — implemented

Implemented or substantially working:

- root/operational node-key lifecycle;
- canonical event bytes and signed transition events;
- authenticated hello and endpoint descriptors;
- real HTTP+JSON transport and node startup integration;
- persistent peer and event state;
- foreground/background database lanes;
- configured seeds, live seed refresh, peer-list exchange, and candidate
  fallback;
- deterministic multi-node fault harness;
- linked-board genesis, posts, self-authored edits, and origin transfer/
  orphan/fork behavior; local materialization both of the board shell and of
  received posts/edits (§9.3, issue #73, closed);
- tier-1 Link messages with accepted/bounced delivery state;
- reliability scoring, relay consent, automatic relay selection, and bounded
  relay mailboxes for outgoing-only recipients;
- issue #60's operational controls and recovery model: backup/restore
  (§13.4, §13.10, issue #75, closed), outbound work items/retry/dead-letter
  for Link mail (§13.7), bounded quotas (§13.9), and startup integrity
  checking, diagnostic log retention, protocol/database upgrade
  compatibility, and graceful Link drain on shutdown (§13.11) — issue #60 is
  closed.
- authenticated inventory/pull-based catch-up and multi-hop relay across
  boards, channels, and file-area catalogues, including empty-inventory
  discovery and responder/freshness/replay binding (§8.8, issues
  #85/#94/#106/#124).
- correctness-preserving `key_transition` retention, and the chain-
  idempotency fix that made any retention provable (§8.9, issue #86,
  closed) — board-scoped types remain intentionally unbounded.
- linked channels — genesis, promotion, materialization, and message
  propagation (§9.6, issue #87, closed); origin succession reused by
  reference only, not built, and moderator governance out of scope
  (Phase 6).
- board closure, origin-authorized moderator post edits, and tombstones
  (§9.5, issue #88, closed); linked-board moderator grants/revocations
  remain out of scope.
- remote file area catalogue exchange and on-demand, resumable, deduplicated
  chunk transfer (§11, issue #89, closed); file-area origin succession
  remains out of scope.
- linked-channel messages wired into the live interactive chat send path
  (issue #91, closed) — closes the gap issue #87 left open.
- interactive browse/fetch UI for remote file catalogues: a
  `[L]ink catalogue` hotkey reachable from file areas, both paginated and
  empty (§11, issue #92, closed) — closes the gap issue #89 left open.
- inventory/pull catch-up extended to file-area catalogues (§11.4,
  issue #93, closed) — closes the gap issue #89 left open; content
  bytes still require an explicit fetch, only catalogue metadata is
  recoverable this way.

Operational validation continuing independently of the development cycle:

- broader real-world multi-node deployment validation (issue #83).

### Phase 3 stabilization status and Phase 4 transition

Phase 3 contains enough tested federation behavior to support Phase 4
implementation. Issue #83's sustained dogfood continues as an independent
operational-validation track: its findings remain roadmap evidence and produce
focused fixes, but its calendar duration no longer blocks the development
cycle. Issue #71's independent non-Python interoperability proof is explicitly
deprioritized and remains open as deferred validation rather than a Phase 4
dependency.

Trust/reputation implementation and the shipped Phase 5/7 features have since
advanced. Phase 6 remains future work. Operational validation remains separate
from implementation sequencing.

The Phase 3 validation record is:

- every currently implemented Link product vertical (linked boards,
  linked channels, remote file areas, and Link mail) has at least one
  end-to-end regression test that exercises the real
  sender/receiver/acknowledgement or sender/receiver/materialization
  boundary across a restart, not only isolated unit coverage (issue #80);
- offline/missed-event catch-up exists and demonstrably converges after a
  partition, not only live delivery during an already-connected pass
  (§8.8, issues #85/#94, closed — including discovery from an empty
  inventory when the resource origin is independently known);
- retained event/dedup state has a correctness-preserving retention policy:
  purging the fast dedup cache must not make an old control event
  re-applicable, nor let suppressed or deleted content reappear (§8.9,
  issue #86, closed — `key_transition` alone is purged; every board-scoped
  type is provably still needed and stays unbounded, not silently deferred);
- issue #60's operational controls have been *rehearsed*, not only
  implemented: backup/restore and an upgrade/rollback have each been
  exercised against a real running node at least once beyond their original
  implementation test;
- a sustained real-world multi-node dogfood deployment (issue #83) is ongoing
  independently, with findings converted into issues or worklog invariants
  rather than left as a diary;
- the README, this design document, and the worklog agree on Phase 3's
  actual boundary, and a newcomer can install and run a node from a
  documented path (issues #76, #82);
- known protocol correctness issue #70 is closed. Issue #71's independent
  implementation is deferred: the Python reference implementation and checked
  canonical vectors remain the Link-v1 compatibility authority for now;
  external implementation interoperability remains explicitly unclaimed, and
  every wire change still requires versioning and vector updates.

Advancing development does not imply public federation. Phase 4's persistence,
protocol, enforcement, and UI are implemented; the remaining human/operational
validation in #131 is still a public-readiness gate. The threat model is in §12.

### Phase 4 — Trust, reputation, and public readiness — implemented; operational gate open

Completed product work informed by dogfood includes:

- direct-chat discoverability, single rendering, and field color (#134) —
  implemented: the pinned status row retains `/close` with a compact narrow-
  width form, submitted input is cleared before its committed rendering, and
  identity/message spans are independently sanitized and colored;
- safe line-mode composition and review-before-commit (#133) — implemented:
  the shared line buffer can list/insert/replace/delete submitted lines, and
  local mail, Link mail, and new posts share an explicit editable review state
  before persistence or dispatch;
- truthful single-key yes/no confirmations with Enter defaults (#135) —
  implemented through one shared structured-key primitive without weakening
  generic menu hotkeys; invalid keys retry and accepted choices end their row;
- current-build visual/capability verification plus bounded semantic-color
  polish on named mature surfaces (#136) — implemented: caller and SysOp Who
  share one picker palette; Who, mail, vCards, the session-history screens,
  profile fields,
  picker feedback, and welcome-banner administration distinguish labels,
  values, metadata, success, and failure through shared theme roles; colored
  narrow output is truncated by visible width rather than raw ANSI length.
  The default web login banner visibly exercises truecolor while the
  256-color rendering remains equivalent and readable. Profile diagnostics
  state the transport's detected capability or limitation; the banner preview
  no longer does, since a SysOp read it as developer output (issue #841). A
  custom SysOp banner bypasses the generated showcase. Before sign-in the
  node's own chrome is plain ASCII over Telnet, where CP437 terminals such as
  SyncTERM call from, and Unicode on the web and SSH; a custom banner is sent
  as authored either way (issue #841). Issue #929 is replacing this with a
  character set per session (§3.2). Both
  Telnet's and SSH's initial banners are shown before capability negotiation
  completes -- Telnet's can precede NEW-ENVIRON, and SSH's own pre-auth
  banner (asyncssh's `send_auth_banner`, sent from `begin_auth` before any
  session channel, and therefore any forwarded environment, exists at all)
  has no client capability to read yet either. Telnet's pre-login banner
  renders the same welcome-banner content at the safe 256-color depth; the
  later profile diagnostic reports the real, negotiated result once
  available. SSH's pre-auth banner (issue #203, dogfood report) sends the
  same content as plain text with every ANSI/VT100 escape sequence
  stripped instead -- `SSH_MSG_USERAUTH_BANNER` is shown during
  authentication itself, and real clients (PuTTY confirmed) commonly route
  it through a display path that never runs an ANSI parser over it at
  all, dumping literal escape bytes rather than color regardless of depth
  chosen; no color depth fixes a client that never interprets escapes at
  this stage. SSH's own *post-auth* welcome screen is unaffected and
  keeps full negotiated color, same as Telnet/web. SSH's pre-auth banner
  is shown regardless of authentication outcome, since it is the only
  screen SSH ever gets before the protocol-level handshake either
  succeeds into the authenticated session or fails.

- sectioned, paginated create/edit screens (dogfood report: the main menu's
  grouped, multi-column layout and the Profile/Board/Area/Channel screens'
  own flat field lists read as wildly different levels of polish) —
  implemented: `edit_resource_draft`'s `FieldSpec.section` groups a screen's
  fields under bold uppercase headings in both the value list and the
  hotkey/menu row, opt-in per screen (Profile, Board, File area, Channel;
  every other screen renders byte-for-byte as before). A dense, sectioned
  screen that still doesn't fit the caller's terminal even at its most
  compact menu tier paginates by section — `Page Up`/`Page Down` cycle
  between them, wrapping at either end, while every field's own hotkey
  keeps working regardless of which page is showing (jumping straight to
  it, switching pages to match) and `[S]ave`/`[B]ack` stay reachable from
  every page. An unsectioned screen has no natural page boundary and keeps
  today's behavior unchanged if it doesn't fit. Profile (14 fields across 4
  sections, plus a bio-preview/transport-diagnostic preamble) is the first
  real screen dense enough to exercise pagination in practice.

These shipped improvements preserve Phase 4's security dependencies and
public-readiness gate. Its implemented foundation is:

- formal threat model from issue #55 — specified in §12;
- persisted local trust inputs, projections, probation, and policy evaluation
  (issue #126) — implemented: separate anchors/reporters/domains, node/user
  subjects, dimension-scoped evidence and vouches, transactional state/audit,
  startup reconciliation, recovery hold, and bounded inactive retention;
- signed trust-signal/vouch subscriptions and evidence verification (issue
  #127) — implemented with explicit bounded pulls, immutable carrier storage,
  issuer verification, replay/freshness checks, and verified digest evidence;
- enforcement across Link transport, sync, relay, content, and users (issue
  #128) — implemented at pre-persistence admission, outbound selection, and
  read-time materialization/display boundaries with stable public reason codes;
- SysOp explanation, overrides, and recovery workflows (issue #129) —
  implemented in the shared SysOp System menu: configuration, effective-state
  explanations, mandatory-reason overrides, recovery, audit history, and
  visibly flagged/confirmed category-scoped sole-authority exceptions;
- remote attestation authority and local acceptance policy (issue #130) —
  implemented with per-attribute opt-in, signed/revocable records, separate
  authority scopes, fail-closed local projections, resource gates, and
  reasoned SysOp explanation/override workflows;
- adversarial distributed validation and the public-readiness gate (issue
  #131) — automated §12.10 evidence is tracked in
  `docs/NetBBS-phase4-readiness.md`; real-node manual recovery,
  independently administered multi-node validation, and sustained private
  dogfood remain pending, so the issue and public-readiness gate remain open.

No public/untrusted federation claim precedes this phase.

### Phase 5 — Real-time Link chat

- Implemented: authenticated Noise transport and live linked-channel chat
  (§8.10, #148), node-wide presence (#164), bounded trusted scrollback-on-join
  (#194), live private messages and relay (#168/#219), and chained relays plus
  cross-node `/private` (#270).
- Not implemented: cross-node `/dm` invitation chats.
- Deliberately deferred: simultaneous channel memberships with background
  delivery; the existing durable unread model remains available.

### Phase 6 — Advanced Link governance and Link Communities

- linked-channel signed membership/topic governance and origin succession;
- Link-blanket moderator grants and authorized moderation events;
- advanced creation, closure, and lifecycle surfaces;
- Link Communities and signed Community membership/carry changes;
- curated governance audit board and live activity feed.

### Phase 7 — Doors and legacy compatibility

Implemented (issue #172, closed — supersedes #63 and #167, both closed;
full design record on those two issues' own comment threads, summarized
here since #172 is self-contained):

- subprocess isolation under the same OS user as the main NetBBS process
  — no dedicated door-runner user, no privilege-drop helper, no root
  requirement (deliberately chosen: operational frictionlessness for a
  SysOp outweighs the defense-in-depth a dedicated-user model would buy);
  `resource.setrlimit()` (CPU, memory, process count) set before exec,
  plus an async wall-time watchdog and unconditional reap via the owning
  async task on every exit path (crash/timeout/normal exit/disconnect),
  matching this codebase's standing "creator cancels, gathers, retrieves
  failures" convention;
- a versioned, deliberately minimal v1 API, drop-file-shaped rather than
  a live protocol: static session metadata (handle, stable numeric user
  ID, terminal width/height, color-depth capability, node name) written
  before spawn; stdio is pure raw passthrough for the session's
  duration, with no framing or control messages interleaved; exit code
  is the only completion signal;
- terminal size is followed for the duration of a run (issue #468),
  without interleaving anything into that passthrough stream. A PTY
  door's own terminal is resized and its group signalled with
  `SIGWINCH`, exactly as any full-screen program already expects. That
  signal may arrive more than once per resize (issue #586): the kernel's
  own delivery to the terminal's foreground group and NetBBS's explicit
  one to the process group, kept because a door which never made the PTY
  its controlling terminal hears only the second. Detecting which case
  applies is racy, and a spurious extra `SIGWINCH` is something every
  full-screen program already tolerates from a real terminal, so the
  documented contract is an idempotent handler, not exactly one signal. A
  stdio or socket door is notified only if its profile opts in, by
  republishing the same static metadata file with the new geometry and
  signalling the door leader with `SIGUSR1`; opt-in because that
  signal's default action terminates a process which does not handle
  it. The one exception is NetBBS's own doors (issue #645): the bundled
  catalogue records which of them handle the signal, and the runtime
  signals such a door without any opt-in when the script a registration
  launches resolves to this install's own copy, or names it as a module.
  Host and door ship in one wheel, so the handler is in the script; a
  copy of the script elsewhere may be any version and follows the profile
  like anybody's door. A handler being in the script is not the same as
  its being installed: that takes an interpreter start and the compiling
  of a ten-thousand-line file, most of a second on a small host. So the
  node ignores `SIGUSR1` in its own process before it spawns such a door.
  An ignored disposition is inherited across `fork` and `exec`, which
  closes the window for the launcher and the door alike; the door
  replaces it when it installs its handler, and a wrapper that does not
  `exec`, or an older copy run as a module, merely never hears it. This was chosen over setting
  the profile flag from the door gallery because a bundled door is
  usually registered with no profile at all, and because it needs no
  migration for doors already registered on running nodes. The doors'
  handlers only set a flag, which an action bar notices within a quarter
  of a second. A profile which pins width/height, a DOS door and a remote
  service are each left alone. This supersedes v1's original "no live
  terminal-resize propagation" rule, which matched every classic door's
  static 80x24-era assumption; the metadata itself stays a file rather
  than becoming a live protocol;
- a door may declare **one** long-lived companion service (issue #466), for
  a game whose world must keep running while nobody is connected. This is
  the single exception to "a door is one process per caller", and is
  deliberately not a general process manager: one service per door, no
  inter-session channel inside NetBBS (the service's own socket is the
  channel), and no privilege separation beyond what native doors already
  have. It starts with the node or on the first caller, under the service
  account, in the door's installation directory, with the same narrow
  environment rules as a door launch. Its own memory ceiling applies and no
  CPU-seconds ceiling does, because a long-lived process legitimately
  accumulates CPU time. Exits restart with lengthening backoff behind a
  circuit breaker, after which NetBBS reports the door's service as failed
  rather than respawning a misconfigured program indefinitely. A caller is
  admitted only while the service is up, has been up long enough to mean it,
  and passes its optional health check; otherwise they get one line and the
  door list back. Stopping is bounded at every level — SIGTERM, the
  configured grace, SIGKILL, then a deadline after which an unkillable
  process is abandoned rather than delaying node shutdown — and services
  stop before listeners and background tasks. NetBBS supervises the process
  only; installing it stays the operator's, exactly as for the door itself.
  A door's installation directory is outside the node's own state and is not
  backed up by default, because it is operator-owned and unbounded in size; a
  node-level setting includes every door's installation in each backup for an
  operator who wants one artifact holding everything. Capture only — restore
  never writes such a directory back, since putting a game installation back
  over a live one is a deliberate operator action, not part of restoring node
  state;
- door output (stdout) is trusted and relayed unmodified, like a SysOp's
  own welcome-banner file, not run through the chat/post sanitizer —
  NetBBS provides the interface and best-effort abuse prevention within
  its own infrastructure, but the SysOp who chooses to run a given door
  is the one vouching for it, the same posture Phase 4 identity
  attestation already takes;
- SysOp registration (path, name, description) and attachment to a
  board/community, reusing the existing file-area-style permission-level
  gating; caller-facing launch and interactive play across Telnet, SSH,
  and web; door-session start/end audit-logged through the existing
  moderation/audit-log mechanism — no new subsystem;
- three real bundled doors ship as installed package data
  (`netbbs.doors.bundled`, not loose example files), proving the
  pipeline end to end: Retro Trivia, a deliberately stateless one-off
  round that is the floor a door must clear; War Dialer, a persistent
  competitive world shared by a node's callers; and Voidrunner, a
  persistent Elite/Trade-Wars-style space-trading and exploration game
  that grew substantially past its own proof-of-concept scope across
  several post-launch feature and hardening rounds (economy, missions,
  combat, a futures exchange, crew, faction reputation, notoriety/patrol
  encounters, ship progression, retirement/New Game+, and a systemic
  fix for a box-alignment overflow bug that turned out to affect nearly
  every screen in its tactical HUD — v5.4.0 release notes carry the full
  list, not repeated here).

Voidrunner saves retire their legacy paths at schema version 2 (issue #421).

The save validator rejects unknown fields and neither `schema_version` nor
`galaxy_version` has ever moved, so "additive only" has been the sole
compatibility strategy: every feature added since launch carries a runtime branch
for careers that predate it, spread through domain and UI code rather than
isolated behind a migration. There are eight such shapes -- the pre-tactics fight
rules and the `tactics is None` forks, legacy futures contracts and their price
multiplier, cargo with no recorded acquisition cost and the `uncosted_*` ledger
fields, duplicate mission ids, sequential squadrons without a `formation` record,
and the pre-`scores/` `leaderboard.json`.

There is no migration. `SCHEMA_VERSION` becomes 2, a schema-1 career is refused
with a message that says the save predates this version and a new career can be
started, and the eight runtime branches are deleted so the validator can require
the new shapes. This is a door game whose affected population is a handful of
pre-overhaul careers on a handful of nodes; a migration for them would be more
code, and more code that can go wrong on someone's save, than the thing it
protects. Hall of Fame records are the exception worth keeping, because they
outlive careers by design, including for pilots who never launch again: a
`leaderboard.json` predating `scores/` is imported in full, every row of it, and
the old file is retained. Because that writes files other sessions own, the
import runs under the maintenance gate -- the same exclusion a restore takes, and
the one thing that guarantees no pilot session is aboard -- and a launch that
cannot take the gate skips the import rather than racing a live checkpoint. It
belongs to the launch, not to a career: it runs before the career is loaded and
whatever the load then decides, so a node whose only returning caller is refused
still imports its rankings. It is complete-or-retry: attempted on each launch
until every row has been written, so neither a busy node nor a transient error
strands a historical ranking.

The refusal is a first-class outcome, not an error: it names the reason, changes
nothing by itself, and offers to begin a new career. Declining leaves the file
exactly as it was, so a caller who wants to fetch the old build first can. There
is one active save slot per pilot, so accepting the offer takes it: the schema-1
document is retained as a recovery copy and the new career is written in its
place. Not `.previous`, as first planned -- `.previous` is the *preceding
checkpoint*, so the launch tick that follows registration would overwrite it and
the retention would have been a promise good for one turn. The recovery namespace
is the one that is never removed, and is already where a rollback puts the career
it replaces. Failing to archive fails the replacement, because taking the slot is
not worth losing what was in it. That is a deliberate replacement of a
readable-but-unsupported career, not the case `write_save` refuses, which is
overwriting a career it cannot read at all.

Shipped as a single slice: `SCHEMA_VERSION = 2`, the refusal path, and the
deletion of the branches it makes unreachable, together, because a half-retired
legacy path is worse than either end state. What the validator now requires,
rather than tolerating and repairing, is the list itself: every fight carries its
tactical state and the hull it started the exchange with, every futures order
names its station, its goods cost and the stock it reserved, every unit in the
hold has an acquisition-cost lot, active contract ids are unique, a two-raider
encounter carries its formation record, an economy event names the stations it
reaches, a career records the contraband milestone step it was awarded under, and
the four fields a pre-overhaul career could omit are present. The repairs those
shapes needed are gone with them: no per-turn id renumbering, no `uncosted_*`
ledger totals, no second set of fire and evasion rules, and no leaderboard read
on the commit path. The id counter's own maintenance stays, because the counter
is derived state rather than a saved invariant, and a posted offer keeps its id
after acceptance, so uniqueness is a property of the active contracts and not of
the board.

Splitting the module into a package
(`domain.py`, `save.py`, `ui/`) is a follow-up to that slice; the launcher
resolves the door through `resolve_bundled_door_path`, which would need a package
entry, and the tests import the module by path.

Voidrunner's showcase development is tracked in issue #310. Completed station
actions (including trading, equipment, crew, contracts, scans, faction rewards,
landmarks and retirement) are committed before their success acknowledgement;
leaving a nested menu is not a prerequisite for saving. Each completed
route hop commits before another departure can be chosen. A failed checkpoint
stops play and retains an explicit error instead of accepting further actions.
Cancelling initial career launch creates no new career, invalid customs keys
make no changes, and the shipyard's displayed upgrade letters work directly.
Interrupted journeys resume before station access. Departure costs and turn
advancement, encounter selection, opponent damage, combat outcomes, mission
payouts, docking, and customs decisions commit with their corresponding progress
state before narration or further input. A restart preserves the event RNG state
and cannot reroll an encounter or duplicate a reward. Only the interrupted hop
resumes; the pilot can plan the remaining route after resolving it. Existing
galaxy seeds stay compatible; schema-1 careers are refused at load rather than
carried forward, as described above.
Career loading validates the supported schema and galaxy-generator version,
record shapes, types, references and gameplay ranges before any startup write.
Missing additive fields retain their legacy defaults; unknown structural fields
or unsupported versions stop play rather than being silently stripped. Galaxy
generation remains version 1 with its original seed/RNG call sequence.

Unreadable or invalid careers enter a read-only recovery screen; they are never
renamed or replaced by an automatic new career. Each changed checkpoint retains
the preceding readable file at `<user_id>.previous.json` before replacing the
current save. Rewriting an identical checkpoint does not age this copy. If the
current file is missing but a previous copy exists, recovery is still required.
The screen shows the candidate's callsign, day, credits and whether travel is
pending. Restoring it explicitly rolls back progress to that checkpoint, requires
a final confirmation, and first retains the original bytes in a unique recovery
copy. Back/EOF writes nothing. Unreadable or oversized originals, invalid previous
copies, versions this build cannot recognise and failed preservation require
manual SysOp repair; there is no caller-facing reset that bypasses preservation.
A career from a *known older* schema is the one exception, because nothing about
it is in doubt: it is offered a new career instead, and accepting still preserves
the old document, as a recovery copy, rather than bypassing preservation.
Recovery copies are limited to eight per pilot; a SysOp must archive older copies
manually before another restore, and a refused career is not replaced at all once
that limit is reached. The pilot session lease covers recovery as well as normal play.
This local previous-checkpoint copy is not a substitute for node backup coverage.

The optional Pilot Guide is reachable from the station deck without changing
state. A new pilot still docked at Freeport on day zero may accept one introductory
delivery to an actual connected station. Its legal cargo follows that station's
demand; the quote includes procurement, a return fuel reserve, crew wages, gross
payment and destination danger. No galaxy RNG calls or generated systems change.
Acceptance is explicit after paginated terms, consumes an ordinary active-contract
slot and tracks the objective. The job has no deadline, uses normal market and
delivery rules, and cannot be taken again after completion or abandonment in the
same career. Its payment covers the quoted cargo and round-trip fuel/wages plus
200 credits; changing prices, detours and encounters can change the result.
The guide explains buying cargo, jumps advancing game time, refuelling, first
upgrades and encounter choices, and retains a recap of active commitments for
returning pilots. Reading or declining it neither writes a save nor advances time.
Accepting First Flight returns straight to the station deck, which shows the next
step (market, quantity, chart destination) as a result line (issue #415).

What the game has to say before its first screen is part of that screen, never a
line written ahead of it: every screen begins by clearing the terminal, so a line
printed first is erased before it can be read (issue #641). The first Command
Deck of a session therefore opens with the pointer to the Pilot Guide for a new
pilot, or with a welcome and the recap of commitments for a returning one, shown
once in the place results are shown. A pilot whose last session ended
mid-journey is told so on a screen that waits for a key, since the encounter's
own first panel follows it. A new career after retirement, and a career rolled
back to its previous checkpoint by the recovery screen, announce themselves on
their first deck the same way.

Escape answers No at every yes/no confirmation and cancels a quantity field,
erasing what was typed (issue #413); prompts say so. Unsupported keys still do
nothing at either prompt.

Voidrunner uses one UTF-8 and terminal-key decoder for menus and text fields.
Unsupported special keys, terminal control strings, and bracketed paste cannot
become menu commands or confirmations. Partial sequences survive transport gaps;
a lone Escape is harmless and does not block indefinitely. CRLF submits a field
once. Callsign editing supports combining accents and wide characters, bounds
both character count and display columns, and normalizes submitted text to NFC.
Quantity fields accept ASCII decimal digits. Unsupported keys do not dismiss
result acknowledgement pauses. Responsive layouts and broader retained-result
presentation remain separate work in #310.

Voidrunner's Back key is `B` on every screen below the station deck, and `B` is
never a live action anywhere (issue #400). List screens allocate their selection
letters from `choice_letters`, which skips `B` and the screen's own hotkeys;
bribes are `P`, the combat information toggle is `I`, and derelict boarding is
`S`. `Q` stays accepted as a quiet alias for Back where no action uses it; on
the deck `Q` saves and disembarks, and the deck's `B` opens the read-only Mission
Board. Choosing a connection on the direct chart is the one irreversible chart
action, so it ends with a yes/no confirmation that states the fuel cost, the
destination's danger (or unknown) and that a day passes; No retains the chart
with a cancelled-departure result and writes nothing. Contract and route jumps
keep their existing preview screens and deliberate Jump actions.

Every hotkey the game prints is written `[K] Label` -- one style, everywhere
(issue #400). The game used to mix three: `[B]ack`, `[B]Back` and `[B] Back`,
sometimes two of them in one action bar. Of the two styles the report named,
`[B]ack` is the one that cannot be carried through, because a hotkey is not
always the label's first letter and is not always one letter: `[1-9] Details`,
`[<>] Page`, `[M] Commodity Market` and `[X] Expand` have no inline spelling.
So `[K] Label` it is, and it was already what every dynamic list row and every
combat detail line used. The bar's label is the word the detail line leads with,
so no action is named twice; that is what renamed the combat verbs Brace to
Guard and Bribe to Pay bribe, and it is why Guard rather than Brace, since `B`
is Back everywhere and a stray Back must never spend a combat turn. The saved
field stays `brace_ready`. The style costs a narrow terminal a row: at the 40x12
floor a bar of five hotkeys wraps to two rows where the glued spelling took one,
so a page holds one row less and the wrap can fall between a key and its label.
That is paid for by the paginator, which measures the bar it will actually show,
and it is worth one row not to make the player learn two spellings of the same
thing.

At every action bar -- service pages, the chart, the mission board, the star
map, the route planner and the draft editors alike -- whitespace and unsupported
terminal keys are absorbed at the prompt rather than reprinting the page; an
unknown hotkey still redraws so the action bar is shown again (issue #416).
`read_command_at_prompt` is the single reader for a command key, so the rule
cannot hold on some screens and not others. No screen may therefore treat whitespace
as a hotkey: the Pilot Record and Hall of Fame previously left on an unlisted
Space, which both cost a redraw for every stray keypress and contradicted `B`
being Back on every screen. Counts in prose are pluralised and the board says
"New offers on day N".

Every paged Voidrunner screen shares one paginator (issue #418). A screen names
its content as groups -- a wrapped line, an authored silhouette, a keyed list
entry -- and the paginator keeps a group whole where it fits, continues it on the
next page where it does not, and never repeats a selection letter on one page.
Prose screens are the degenerate case: every row is its own group. Capacity is
measured once, from the wrapped title and the action bar the caller will see.
Page movement is clamped in one place, for both spellings of the paging keys
(`<`/`>` and `P`/`N`), and a screen pages with the pair it advertises, because
`N` is a live action on screens that do not page with it. A screen that measures
or clamps for itself will disagree with the rest at some terminal width, so it
must not.

The loop itself stays with each screen. A single driver owning Back, paging and
dispatch for every paged screen was considered and rejected: it would rewrite the
control flow of some thirty screens in one change, against a test suite that is
their only safety net, to remove one line per screen that the shared page step
already removes. The paginator is the part that was genuinely duplicated.

Route planning starts from something real (issue #415): the chart's route
planner opens its destination picker at once instead of an empty screen, the
ledger lists Opportunities first, and the ledger's route draft pre-fills from the
best remembered lead when one exists. Counts in prose are pluralised.

Contract details state only the notes that apply (issue #412): the cargo-basis
note when cargo of that commodity is already aboard, the crew-wage note when crew
is aboard, the survey note for survey contracts whose target is still uncharted,
and the unknown-danger note when a leg is uncharted; the general reminders live
in the Pilot Guide. A blocked survey says only that revisiting cannot complete
it, because scanning cannot complete it either. Every offer's earlier pages say
where Accept is instead of hiding it, First Flight included. One-page screens
drop Prev/Next from their action bar but keep the page counter, and a screen is
paginated against the bar it will actually show, so dropping the tokens can be
what makes it a single page. Market rows carry no filler status word, the
exchange reminder appears once in the catalog, and a row that would overflow the
terminal gives up its depth figures first and its hold count only as a last
resort -- never a truncated word -- so a tagged contraband row still reads as
one row, and a terminal too narrow for even the shortest form keeps the whole
row and wraps it.

A hop's narration is retained for the next deck page (issue #410): the departure
charge itself -- fuel burned, fuel left, crew wages paid and credits on hand --
then events, discoveries, encounter and combat outcomes, customs results,
settlements and mission completions appear as `Result:` lines on the first
Command Deck page after the jump, and are cleared by the next deck action. The
report is session state, never persisted and bounded to the newest eight lines
so the deck still fits 24 rows. A journey resumed after an interruption
therefore reports the phases it actually replays, not the ones that completed
before the interruption: narration is a convenience for the caller who is
sitting there, and is not worth a save field or a resume-consistency rule of
its own.

Posted deliveries scale with the pilot's hold (issue #408): a board draws the
quantity between three and the larger of ten and two fifths of cargo capacity,
so a Shuttle's boards keep their existing range while a Carrier sees bulk
contracts whose reward scales with the quantity. Spot-stock pools do not limit
contract cargo, which gives large hulls work that depth cannot throttle. A
board is therefore reproducible from its seed, station, day and the pilot's
hold rather than the first three alone; posted boards are persisted and are
never regenerated under a pilot, so offers already on a board never change.

Completed legal contracts (deliveries, surveys and escorts) earn one point of
Concord standing each (issue #407), so a contract-running career reaches the
commission without combat; lost or expired contracts earn nothing. Combat and
story rewards are unchanged. A bounty pays its Concord standing through the
kill, not the contract. The award is stated where the player meets it: contract
terms name it before acceptance, the completion message and pilot log name it
when it is paid, and each faction contact screen names how its own standing is
earned.

Contract boards retain up to four posted offers per station for three game days
from generation. Station checkpoints prepare offers and expire old jobs before
the caller opens the board; browsing and backing out write nothing. Viewing or
accepting offers does not replenish them; jumps
advance game time. Posted terms and consumed offers survive restart. New careers
may hold at most three active contracts; legacy over-limit careers retain every
job but cannot accept more until below the limit. IDs are unique across active
jobs and cached offers, and the validator requires it: a document that repeats one
is refused rather than repaired.
Deadlines are inclusive: a contract is eligible on its deadline day, expires
before rewards or mission encounters on the next day, and is never paid late.
Already-charted survey offers are unavailable. Selecting a posted or active
contract opens its complete terms without writing. Details include the named
target and coordinates (without charting it), minimum hops and first bearing,
inclusive deadline, cargo procurement and hold needs, gross payout, and estimated
remaining cash outlay at current prices. Estimates explicitly exclude sunk cargo
cost, repairs, detours and market changes. Escorts warn of a fight on every jump,
including detours and other concurrent escort jobs. Survey contracts use a
SURVEY label and explain that arrival or a scanner discovery can complete them.

Acceptance is a separate action on the final details page. One active contract
may be tracked, persisted across sessions and shown at the station and chart.
Tracking reveals its bearing, not remote market or danger information. Abandonment
requires a final confirmation, forfeits the reward with no extra fee, retains
cargo, and does not repost the offer. Completion, expiry and abandonment clear
tracking. Contract lists and detail text paginate to terminal dimensions, with
Back on every page. Broader map/route planning remains in issue #310.

Contract details offer a read-only Route screen, including for posted jobs before
acceptance. It plots every connection to the named target, while uncharted
intermediate names, economy and danger remain hidden. The contract target's name
and coordinates are public terms, not a chart discovery. Active routes account
for queued bounty re-entry, fuel, wages, refuelling stops and inclusive deadlines.
The chart also opens the currently tracked contract route directly.

Jump next is available only for a current, unexpired active contract while docked
and with fuel for that leg. A survey target must still be uncharted: discovery
retained from a destroyed journey cannot be repeated for a reward, so its contract
is explicitly blocked and can be abandoned. It tracks the selected contract and checkpoints that
intent, then uses ordinary durable travel for exactly one jump. The pilot chooses
again after each outcome; diversion, completion, failure, expiry, fuel or crew
changes cause a fresh route/budget. No unattended remainder is resumed after a
restart. Browsing, Back and EOF do not track, accept or travel. At the destination,
delivery shortages are explained; bounty jobs still require ordinary re-entry
rather than a fabricated in-place encounter. General named-destination routing follows the same one-hop control described below.

General navigation opens a screen before asking for a destination. Its paginated
picker lists charted system names; uncharted contract targets remain available
through their contract route. A selection is a temporary route preview and writes
nothing. Back, destination cancellation and EOF leave the career unchanged.
The preview shows every leg, unknown threat labels, fuel, wages, manual refuelling
stops and active-contract deadline estimates. Jump next validates and flies one
ordinary durable leg, then retains the result and recalculates. Refuelling does
not need to fit the whole journey into one tank. Choosing a new destination or
leaving the screen interrupts the plan; selection must be made again after Back
or restart. No multi-hop execution or yes/no dialogue remains in this screen.

The spatial star map is a read-only view of the existing seeded coordinates and
connections; it does not change galaxy generation or save state. It opens in the
current sector with next/previous sector navigation and a galaxy overview. Known
stations, the current position, tracked objective and an explicitly plotted route
have distinct ASCII markers, with a paginated list alternative and exact-coordinate
station inspection. The list is always available; the spatial map needs the room
the 40x12 floor guarantees.
Projected cell collisions are labelled; the exact list/inspection remains authoritative.
Only discovered station details are public, except a contract's named target.
Uncharted route points show bearings and unknown danger. Ordinary connection lines
join charted endpoints; the plotted path can cross uncharted space without revealing
other links there. Contract/general navigation opens the map with its current path.
Map Back returns to the same route; inspecting never charts, tracks or travels.

Engineering-yard and crew screens paginate by wrapped rows and negotiated height.
Narrow screens stack upgrade/crew terms instead of wrapping padded table borders.
Keep each label, price and benefit together when the entry fits a fresh page.
Credits appear in the page heading; fuel, hull, hire costs, ongoing wages and refit
terms remain available in full. Direct action letters keep their meanings across
pages, with previous/next punctuation keys and Back always present. Completed or
rejected actions retain their result at the start of the refreshed screen. Back,
paging and cancelled action prompts do not checkpoint or spend. Confirmation is
only behind a chosen purchase/dismissal action, and existing durable action
checkpoints still precede success acknowledgement.

The direct-jump chart uses height-aware connection entries rather than a fixed
79-column table. Fuel stays in the heading; each connection retains its bearing,
known sector/economy, danger or unknown label, jump cost and tracked-next marker.
Entries remain together when they fit a fresh page, and overflow can always be
browsed with previous/next controls and Back. Standard connection keys stay stable;
exceptionally dense charts reuse the reserved-key-safe alphabet only on later
pages. Rejected departures and scan results remain visible on the refreshed chart.
Browsing does not change the career; scans and travel retain their existing rules.

The station command deck uses the same bounded paragraph pagination as service
screens. Credits and Exit remain in every page's controls; service hotkeys keep
their meaning even when their labels are on another page. Compact telemetry uses
explicit hull/fuel fractions and cargo used/capacity, without treating a full hold
as healthy or an empty hold as damage. Contraband and economy-event warnings can
appear together; low fuel, critical hull, unaffordable wages and the tracked
objective provide contextual next actions. Expand adds pilot, location, commitment
and achievement details for this visit; it is a read-only view toggle, not a saved
career option. Paging and invalid keys do not repeat station settlement. Completed
settlements retain their result in the deck after the existing durable checkpoint.

Pilot records provide direct Overview, Contracts and History views with Back and
pagination on every page. Contracts retain complete descriptions, public target
coordinates, inclusive deadlines and reward context; the mission board remains
the place for contract actions. History exposes every already-retained highlight
and log entry newest first, without extending storage retention or silently hiding
earlier entries behind a display limit. Read-only pages are cached per view for
the visit so large legacy records are not reformatted on every key. Retirement
uses the Finale screen, final chosen-action confirmation, new-career
bonus and durable checkpoint. Cancelling retains the record with a visible result.

Pilot rank is lasting recognition within the current career. Each completed-action
checkpoint records any crossed balance threshold in the existing highest-rank
field, with a promotion highlight and log entry, before another menu action can
spend that balance. Displays and retirement eligibility use the greater of this
retained rank and the current balance rank. Purchases, fines and salvage cannot
demote the pilot. Rank terms show the next balance threshold and distinguish it
from current funds; a legacy promotion is retained even when credits are lower.
A lifetime leaderboard high-water mark must never promote a new career. Retirement
resets rank as before; subsequent starting-credit bonuses are ordinary current
funds. Old careers without a recorded promotion can establish only what their
current balance proves. Read-only rank views do not write or fabricate history.
Promotion notices do not introduce a separate acknowledgement dialog.

The pilot record exposes three independent accomplishment paths as well as the
balance-based rank. Trader stages use recorded known-cost market-sale margin at
5,000/20,000/50,000 credits; delivery pay, unknown-cost receipts and non-trading
income do not count. This is cargo margin, not total career profit. Explorer
stages require 12/30/all 48 charted systems. Combat stages require 5/20/50 recorded
victories. Progress is factual and may fall where its underlying measure falls;
current-career rank remains permanent. These paths unlock different career
conclusions without requiring the 150,000-credit top rank. That threshold is set
so the last rank does not become an empty stretch: a greedy trading career
finishes every ship purchase and the trader finale by roughly day 45 and reaches
Void Baron near day 84, and the remaining climb to Legend must not be scores of
identical jumps with nothing left to buy. A wealth-scaled sink or goal for the
late game remains open work, and human playtesting decides whether one is
needed. Earned ranks are retained, so lowering the bar only ever promotes.

The always-available record Finale screen shows all four conclusions and their
requirements before choosing: Frontier Legend at retained top rank, Trade Guild
Founder at 50,000 recorded market margin, Atlas Keeper with a complete atlas, or
Frontier Warden after 50 victories. Selection is a read-only draft. Only the
chosen Retire action followed by final confirmation ends the career. It records
a distinct closing account and begins a fresh galaxy, retaining the existing
500-credit-per-retirement bonus. The specialist conclusions also start the next
Shuttle with one ordinary cargo/scanner/weapon tier respectively; the Legend
conclusion keeps the ordinary starting ship. No other gameplay progress carries
over. Thus the chosen conclusion determines a concrete starting advantage.

Retirement retains a compact dossier in the same atomic career checkpoint as the
new run: sequence number, original seed/dates, conclusion, rank, ship, days,
credits, chart count, victories, missions, known market margin and retained
highlights. A confirmed retirement ends on a held Career Complete screen (issue
#644), drawn only after that checkpoint is on disk: the ending's name and
closing text, the portrait of the ship that flew the career, the career in
numbers and its last highlights as the archived dossier records them, and what
the next pilot starts with. Any key but a paging key leaves it, Enter, Space and
Escape included. The record's Dossiers view includes every retained dossier. Old
retirement counts do not fabricate missing dossiers. Storage permits 128 dossiers
and refuses further retirement visibly at capacity; it never evicts an older
record. Existing careers remain playable at the cap. The Retire action preflights
the normalized replacement against the existing save-size/schema envelope before
final confirmation; insufficient space retains the current career with an explicit
unavailability result. Accepted legacy highlights are preserved, not clipped to
the live append helper's retention limit. Version checks precede v1 field validation,
with unsupported/new fields preserved for the appropriate build.
The lifetime score remains separate from current-run rank and achievements.

Station Viewport and yard ship portraits expose distinct native terminal artwork
for each of the four hull classes, five station economies and four discovered
landmarks. Damage changes the hull's visual shading while explicit hull/fuel/cargo
figures remain authoritative. Portraits use full and compact authored compositions,
retaining an entire silhouette on one page at supported terminal dimensions; text
and navigation paginate normally. Existing full/basic/mono/plain modes apply.
The plain mode uses ASCII art without color. A portrait is a still composition --
it may be revealed under the motion rules in the presentation contract, but it is
never redrawn -- and viewing one makes no random draw, market observation or
career write. Station captions use actual
places, sectors and specialist contacts. A landmark portrait is available only at
its station or after investigation, not merely through an accepted bearing.
Ship commissioning previews and landmark inspection use these same portraits;
backing out of either view leaves gameplay unchanged.

Hall of Fame offers five separate local achievement views: lifetime wealth,
known-cost market margin, exploration, combat victories and completed careers.
Wealth and completion counts rank pilots; trading, exploration and combat rank
individual current or archived careers. Each view shows at most 20 entries with
stable numeric pilot/career tie-breaking. Full callsigns, values, career number,
seed and current/completed state remain available on paginated narrow screens.
There is no combined score. Market margin has the same exclusions as the trader
career path; combat counts the existing generic victory counter, not raiders only.
Valid legacy/per-pilot lifetime retirement maxima are imported into the career
save before projection, preserving current career numbering and future retirement
bonuses without inventing old dossiers or changing current funds. Legacy wealth
and retirement counts remain valid, but absent career metrics are
labelled unavailable rather than fabricated. The five views share one read-only
snapshot per visit. Switching categories, paging and Back never write scores.

Each pilot's optional score projection contains bounded versioned summaries of
all retained retirement dossiers plus the current run. A checkpoint regenerates
these summaries from the authoritative career save, including after an optional
score-write failure across retirement. No top-20 filter discards stored history.
Legacy retirement gaps remain gaps, and the existing 128-dossier capacity applies;
no separate archive files or gameplay counters are added. Future score-summary
versions are left untouched by older code. Dossiers retain the fuller personal
history; shared score summaries omit their highlights.

Shared-seed competition is deliberately deferred. Existing careers have different
New Game+ advantages and rule histories, so matching seeds alone would imply an
unsupported fairness guarantee. These are local accomplishments, not certified
competitive runs. A future challenge would need its own versioned rules and equal
starting conditions without changing the single-player career core.

The spot-market catalog uses height-aware commodity entries with credits in the
heading, numeric cargo usage, unit buy/sell prices, stock, station buying demand
and held quantities. Commodity letters retain their meanings across pages. Illegal
cargo and regional price-event labels are shown together, and prohibited purchases
at ordinary stations are labelled unavailable in both catalog and details. A manual
unadvertised buy key still rejects without changing state. Completed/rejected trades
return a retained result to the catalog; cancelled quantities preserve its prior
result. Existing checkpoint-before-acknowledgement and trade rules remain unchanged.

Futures catalogs use complete-choice pagination with up to four selections per
page, read-through continuation for oversized labels, and nonselectable fee/limit
notices. Returning from inspection preserves the list page; a completed action
returns to its retained result. Signing drafts and existing-order details paginate
with credits and Back available, preserving quantities/terms through paging and
rejected input. Draft edits write nothing; signing and cancellation retain their
existing final confirmations, fees, pickup behavior and legacy-order terms. Their
durable results remain visible in the futures catalog and on return to the market.

Faction contacts remain accessible at every station through [P] Concord and
[W] Blackwake, before and after membership. These are read-only, paginated screens
until a deliberate Join action and final confirmation. Each shows current standing,
joining requirements, the once-only 2,000-credit grant and exact perk conditions.
Joining validates eligibility before mutation and checkpoints before acknowledgement.

Dual membership is explicitly permitted, including existing careers: Concord and
Blackwake retain independent membership and standing. Neither membership is erased
by hostile conduct, but a faction suspends its own perk at standing -50 or below;
it resumes automatically above -50, without another joining payment. Concord's
active commission adds 25% to bounty/escort payouts, evaluated when they pay.
Blackwake's active membership halves the chance of a new customs inspection.
Already resolved encounter decisions remain cached on reconnect. Membership does
not excuse notoriety, clear fines or override the other faction's standing.

These conditions apply to existing memberships as well as new ones. The pilot
record and contacts show active versus suspended perks, and bounty/escort details
explain that their quoted bonus follows standing at payout. Historical highlights
are retained as records of what was said when earned. Retirement clears membership
as before. Authored faction assignments build on these contacts in a later slice.

Each faction contact offers one optional authored case per career through [S]
Story. Cases require no membership, entry fee, ordinary contract slot or deadline.
After acceptance the pilot visits an existing workshop for evidence, chooses an
irreversible course of action, then visits the stated destination to complete it.
All choices show destination, material requirements, gross payout and current
capped standing effects before commitment. Cargo handovers receive a final
confirmation and use ordinary delivery FIFO accounting.

Concord's Missing Dispatch begins at Rivet House. Filing for prosecution returns
the evidence to Freeport for 1,800 credits, Concord +18 and Blackwake -12. Patient
relief instead delivers two Medicine to Far Lantern for 1,500 credits, Concord +12
and Blackwake +6. Blackwake's Broken Toll begins at Tuning Fork. Armed enforcement
delivers one Weapons to an existing Haven for 2,100 credits, Blackwake +18 and
Concord -12. A public beacon instead delivers three Electronics to Far Lantern
for 1,500 credits, Blackwake +12 and Concord +6. Standing remains within -100..100.
These fixed case rewards do not receive commission bonuses. Each ending records
one completed mission, a named career highlight and a distinct closing response.

A separate faction-stories-v1 namespace selects the Haven without changing galaxy
or encounter randomness; if no Haven exists, armed enforcement is unavailable and
the beacon alternative remains available. Only an accepted case's current bearing
permits travel to an otherwise uncharted case destination. It reveals no market
quotes. Two optional versioned case records retain accepted/evidence/committed/
complete stages and the chosen ending; incompatible or invalid state is preserved
for recovery. Acceptance, evidence, choice and completion checkpoint before their
results. Browsing, refusal and Back write nothing; retirement clears both cases.

Crew are recurring named specialists, with a concise personality and persistent
service record per role. The `crew-v1` identity namespace chooses from fixed
role-specific candidates without galaxy or encounter RNG. Browsing previews a
candidate without creating a record; hiring records that identity. Existing hired
crew without records retain their base bonuses and begin recorded service at the
next paid jump. Earlier service is unknown, never guessed. Wages and hire prices
remain unchanged.

Five, fifteen and thirty paid jumps promote a specialist from Recruit through
Seasoned, Veteran and Ace. Recorded service caps at thirty. A gunner adds one
extra damage per promotion above the base three; an engineer saves 25%, 30%, 35%
or 40% of base jump fuel (savings rounded up, minimum one fuel burned); a navigator
adds one to four survey hops. Unhired crew provide no bonus. Promotions commit
with the departure that pays wages and are acknowledged afterward; engine fuel
is already spent for that departure, so its improved efficiency starts on the
following jump. Route budgets use current efficiency and can conservatively
overestimate later fuel when a promotion occurs en route.

Dismissal or unpaid resignation preserves the named record and service; rehiring
costs the ordinary hire fee and restores the same specialist. Hiring, dismissal
and salary progression validate or resolve synchronously, with UI/travel owners
checkpointing before acknowledgement. Only three optional versioned records are
stored on the ship, with bounded candidate identity and service count. Malformed
or unsupported records enter preserving recovery. Existing destruction/stranding recovery keeps hired crew and their records. A
fresh or retired career has no crew records. Existing interrupted travel never repays wages or grants service
again; cached combat with absent records retains its prior crew bonus.

Each Seasoned-or-better specialist offers one optional personal assignment per
career, reached from the crew roster's numbered task entries. Acceptance is free,
uses no ordinary contract slot and has no deadline. Completing it requires that
specialist to be hired and the pilot to visit the named existing workshop. The
gunner brings one new combat recording to Rivet House for 600 credits; the
engineer delivers three Machinery to Tuning Fork for 900 credits; the navigator
brings up to three newly charted systems to Far Lantern for 700 credits. If fewer
than three systems remain, all remaining systems suffice; a complete atlas can
be delivered directly. Combat and chart progress use acceptance-time baselines,
so earlier accomplishments are not counted as new work. Progress can be gathered
while a specialist is off the roster, but handover requires rehiring them.

Every assignment awards one completed mission and a personal career highlight,
once. It grants no extra faction standing, crew service or module tier. Material
handover uses ordinary delivery FIFO accounting and warns about shared contract
cargo before its final confirmation. Task state survives dismissal and salvage
recovery, and clears on retirement. Optional versioned task records are bounded
and validated with the crew record; no new galaxy/event randomness is consumed.
The detail page shows the complete objective, progress, reward, crew obligation
and public route before any action. Browsing and Back write nothing. Acceptance
and completion checkpoint before their retained result appears.

Three specialist workshops give existing stations distinct outfitting roles:
Iona Rusk's Rivet House expands cargo bays, Oren Vale's Tuning Fork tunes engines,
and Dr. Sel Parn's Far Lantern builds scanners. A separate `workshops-v1` seed
namespace assigns distinct non-Freeport stations, preferring their matching
economies and a few hops from home. This does not change galaxy generation,
ordinary station services or encounter RNG. Their public directory gives route
bearings without charting those stations or creating market observations.

Workshops install the next ordinary module tier for 65% of its normal credit
price (rounded up) plus materials: two Refined Metals per resulting cargo tier,
two Machinery per resulting engine tier, or one Electronics per resulting
scanner tier. Materials and travel can outweigh the cash saving, so the quote
shows the ordinary price, required and held materials, and reminds pilots to
compare total costs. Inputs may include cargo promised to delivery contracts.
The pilot must visit the workshop; maximum tiers and upgrade effects remain the
ordinary module rules. No extra fuel, repairs, reputation or mission completion
is granted. Installation is a deliberate, confirmed action, bounded by module
caps; cancellation, directory browsing and route previews write nothing.

Validation precedes credit/material consumption. Existing FIFO cargo accounting
records workshop materials separately from losses, and records workshop credit
spend separately from trading profit. Legacy ledgers default these additive
counters to zero. The module tier, costs and named workshop career highlight
checkpoint together before success is shown. Career retirement clears these
ordinary module and ledger values. The work order itself adds no day or RNG draw;
travelling to a workshop follows the usual risks, fuel and wage rules.

Voidrunner's optional Archive assignment connects Freeport with the existing
seeded landmark. Mara Venn offers the assignment through Archive Contacts. It has
no deadline, deposit or ordinary contract-slot cost. Accepting reveals a navigable
bearing without charting the destination; recovering the record requires visiting
the site, not merely scanning it. Each of the four existing landmarks has its own
record and interpretation. Previously investigated sites can supply a transcript
without paying their salvage twice.

On returning to Freeport, the pilot chooses public preservation (500 credits,
Concord +5) or a private sale (1,500 credits, Blackwake +5, Concord -2). Standing
is capped within -100 to 100; previews use current standing and results report
the effective change. Each ending awards one completed assignment and a career highlight, once. The contact's closing
response reflects that choice. Optional versioned flags record acceptance, recovery
and mutually exclusive endings; recovery requires an investigated landmark. Every
action validates before mutation and checkpoints before acknowledgement. Browsing,
routing and Back do not accept or finish the assignment. Ordinary travel rules
remain in force and the story consumes no encounter or galaxy-generation RNG.

Landmark inspection itself is screen-first: show its story and unclaimed reward,
then let the pilot Investigate or Back. Existing salvage amounts and seeded sites
remain unchanged; a direct repeated or remote claim is rejected.

Long-range scanning is a deliberate area survey. With a scanner installed, the
chart's Scan action opens read-only terms showing two fuel required, remaining
fuel, range and the number of uncharted contacts. Survey charts every new system
within 2 + scanner tier + navigator bonus connection hops, in stable distance/ID
order. It advances no day and consumes no encounter RNG; no contacts or insufficient
fuel means no charge. Valid survey contracts complete through ordinary discovery
rules in the same checkpoint as fuel spending and chart updates, but a contract a
scan completes pays half its reward (`REMOTE_SURVEY_PAYOUT_PERCENT`); arriving
pays it in full. At full pay a scanner made surveys close to free income -- two
fuel, no jump, no day -- and the contract asks for the trip (issue #648). The
contract details and the scan terms both state the half-pay figure.

The report retains each discovered system's name, station, economy and danger,
plus mission outcomes, using height-aware pages. It creates no remote price
observation. Back before surveying writes nothing and preserves the chart's prior
result. No galaxy generation or existing discovered state changes on load.

Derelict and distress decisions page complete terms with credits, fuel and actions
available on every page. Browsing and invalid input have no effects. Derelict
boarding discloses its 70% salvage / 30% ambush split, credit range and opponent
tier range; new salvage and ambush generation use the destination sector danger.
Cached opponents retain their stats. Distress calls disclose the existing capped
2-4 fuel cost, 60-180 credit reward and up to +3 Concord standing (capped at
100), including an empty-tank warning. Ignoring either encounter continues the journey without a penalty.

New bounty interceptions offer identification in the combat screen before engaging.
Verify costs one fuel and reveals whether the posted identity matches; Withdraw
continues the journey while keeping the contract. A verified mismatch can be closed
without combat, bounty payout, mission credit or notoriety. Firing unverified or
knowingly at a mismatch retains the full bounty but carries the stated notoriety
and Concord-standing consequence. Once engagement begins, identification and free
withdrawal are unavailable. No automatic, undisclosed post-kill identity punishment
is applied to a target already selected under the older rules.

Warrant truth is deterministic per career/contract, using a separate versioned RNG
namespace, and is cached with checked/engaged flags. It never rerolls on inspection,
withdrawal or reconnect, and it consumes no encounter RNG. The historical bounty
completion RNG slot is still consumed after a win for sequence compatibility.
Verification fuel, flags and retained result checkpoint before acknowledgement;
closing a mismatch checkpoints its terminal result before the parent removes the
contract. Validate warrant versions and reported-outcome consistency on load.

New two-raider encounters use a versioned coordinated formation. Before the first
combat decision, Target switches which raider to engage first without spending a
turn. Both opponents and the covering-fire contribution are disclosed. While both
remain, the other raider adds 2 + twice its tier to a surviving target's return
fire, after shields and before Guard reduction. Killing the target prevents that
exchange's return fire and ends covering fire for the remaining duel. Guard
therefore protects against the pair, while target selection can remove a fragile
or dangerous partner first. No healing occurs between foes; successful evasion or
bribery breaks contact with the whole squadron, as before.

Formation metadata is attached when a new pair is generated, and every saved
squadron carries it. Target selection checkpoints
order and combat state before acknowledgement and closes after any valid combat
decision. Formation creation and target changes consume no encounter RNG.

New Voidrunner fights use a versioned tactical ruleset with three deterministic
opponent patterns: Raider (attack, volley, recover), Bulwark (cover, volley,
recover), and Skirmisher (harry, attack, volley). The current intent and incoming
damage range are visible. Fire deals full damage and recharges Guard; Guard fires
a reduced shot and cuts incoming damage to a quarter, then requires Fire before
reuse. Cover/harry reduce outgoing damage, recovery exposes the enemy, and harry
reduces escape chance. Failed evasion/bribery receives and advances the same intent.
New damage curves smooth starter tier-2 fights and make high-tier fire consequential
for heavy hulls; enemy HP is not inflated to lengthen fights. Ruleset version 2
(issue #406) changes the per-tier threat bonus from 0/3/6/20/55 to 0/3/6/8/34:
version 1 dropped a bracing starter Shuttle from a 99% win at tier 2 to none at
tier 3 between adjacent danger ratings; under version 2 that Shuttle wins about
half its tier-3 fights with Guard (and almost none without), a Cutter guards
through tier 4 about half the time, and a maxed Carrier still loses about 40%
of its hull to a tier-4 squadron, so heavy hulls keep paying for fights.

Ruleset version 3 (issue #647) keeps version 2's damage and adds a way out of a
fight the ship cannot win. A raider *outclasses* a ship when two unguarded
volleys would break its full hull; for a Shuttle on its factory hull that is tier
4 and nothing below it, and hull reinforcement is a real answer to it. An
outclassing raider is after the cargo, not the wreck: Dump gives up half the hold,
chosen at random, and breaks contact three times in four, and Evade has a floor
of 30%. Harry still takes ten points off either. The combat panel marks the
contact OUTCLASSED and prices the Dump it will actually make, and the chart's
departure prompt says "Raiders there can outclass this hull" when the
destination's worst raider (its danger plus one) would. Found in play: the first
profitable market of a career sat in a danger-4 system one jump from the First
Flight destination, and under version 2 a starter Shuttle there had Evade at
5-14% and Dump at 24-34% against an attack worth two thirds of its hull and a
volley worth all of it, so the only available lesson was never to go. The rules
are for raiders: a Concord patrol is not after the cargo, takes no Dump and has
Surrender as its way out, so its odds are unchanged and its panel is not marked. In trials a Shuttle with a full hold that
dumps and runs now survives a tier-4 contact 94 times in 100, giving up thirteen
units on average, against 48 under version 2; with an empty hold it is still a
coin toss, which is what the departure warning is for. Rejected: lowering tier-4
damage (it is what keeps a Carrier honest), and moving dangerous systems away
from the opening neighbourhood, which needs a new galaxy version and would make
the risk rarer without making it any more survivable.

A fight keeps the ruleset it started with; every listed version loads, others
are unsupported. Automated trials establish the curve; human playtesting still decides its feel. Existing rewards,
ship upgrades, faction consequences and squadron sequencing remain in force. Newly
generated raiders use destination danger for both squadron chance and tier; stored
opponents retain their stats, and tier/name RNG draw ordering stays unchanged.

The tactical pattern step and Guard readiness checkpoint with opponent HP and the
last exchange. Pattern selection consumes no extra RNG. Every fight uses the tactical
rules and every saved fight carries its tactical state. Reject malformed or
unknown tactical versions through preserving recovery. Automated probes establish bounded fight lengths and
mechanical tradeoffs; human playtesting remains required for engagement/balance.

Voidrunner customs inspections show complete surrender/bribe terms in height-aware
pages: detected contraband, current credits, the conditional bribe payment and
60% acceptance chance, confiscation/fine on refusal, and standing/notoriety effects.
A fine is capped at credits on hand and creates no debt. Unaffordable bribery is
unavailable; typing its key is harmless and retains the inspection without RNG,
cargo, credit or reputation changes. Valid decisions keep the existing outcomes
and checkpoint their effects and completion before acknowledging them. Browsing
and invalid input preserve the pending inspection across disconnect/restart.

Voidrunner combat uses height-aware pages with numeric opponent/hull, fuel and
cargo telemetry and current credits in the heading. The last checkpointed exchange
remains available while paging or toggling tactical details. Browsing consumes no
combat turn or encounter randomness. Escape odds, conditional bribe payment and
refusal retaliation, random one-unit cargo sacrifice and patrol surrender terms
are visible before choosing an action. Existing action hotkeys work across pages;
browsing preserves encounter RNG and resumable state. The combat action bar
labels every verb like the other detail screens (`[F] Fire [G] Guard [E] Evade
[D] Dump [P] Pay bribe ... [I] Info`) instead of a bare letter list, and Dump appears
only with cargo aboard; with an empty hold `D` is not a displayed action and
does nothing (issue #414). Every verb is named at every supported size: the
compact letter list this kept below 40 columns went with those terminals
(issue #495).

Every path that ends an active contract without payment records it (issue #403):
failed escort or bounty fights and abandonment count as failed, deadline expiry
as expired, each once and in the same checkpoint that removes the contract, with
a log line naming the forfeited reward; contracts still active at retirement
count as abandoned in the dossier and are named in the new career's log, the
finale screen says how many, and the retiring save is not mutated so a
cancelled retirement leaves nothing behind. Closing a verified-mismatch warrant is neither. The pilot record shows completed, failed and expired; dossiers written
from this version carry both counts (older dossiers carry neither and still
load). During an escort fight the Evade, Dump and Bribe lines say the contract
fails.

Destruction charges a salvage fee of 200 credits plus four per point of maximum
hull, capped at credits on hand so it never creates debt; it loses all cargo and
tows the ship to Freeport (issue #402). A fee paid in full restores full hull; a
pilot who cannot pay it all gets a quarter of maximum hull plus the paid fraction
of the rest, which stays below full hull until the whole fee is paid, so an
empty account buys a flyable ship rather than a repair. That floor never rises
above the hull the ship carried into the fight: the tug patches, it never
upgrades, so a wreck cannot hand a broke pilot hull the yard would have charged
for. The fee is always dearer than repairing before the fight, so losing is
never the cheapest repair at any balance.
A Concord patrol collects the outstanding notoriety fine on top of the salvage
fee and closes the file only when both are paid; an unpaid fine leaves the
wanted status standing, and a raider kill never touches notoriety. Surrender
therefore remains the cheaper way out of a wanted status at every notoriety
level, because it costs the fine alone and keeps the cargo. The combat page
names the fee beside its low-hull warning, the Info view states both rules and
the fine a patrol will collect, and the deck's critical-hull line names the fee.

The title splash and the registration box follow the portraits' rule (issue
#404): the large composition with the block logo appears only when it fits the
negotiated width; otherwise a complete compact composition (wordmark, wrapped
subtitle, stacked node/pilot/galaxy fields) is shown, and every box row has one
display width, including for callsigns with wide characters. The registration
greeting is drawn in one color for the whole of its text, with the callsign the
only part picked out: a sentence that wraps must not change color between its
rows, and body text inside a box is never left in the terminal's default
foreground, which on a caller's client can be the color the box border itself
is drawn in.

Every paged screen draws inside the tactical HUD's box (issue #486). Making the
screens responsive to a negotiated terminal had spent the frame, the cockpit's
status band and its gauges, which no issue had asked for; the box is back in the
one place all twenty-four paged screens draw through. Its cost is part of the
page-capacity measurement -- one row for the bottom border, and four columns for
the sides, the indent and a right-hand gutter -- so a framed page still fits the
height it was negotiated for, and a keyed entry is wrapped narrower again to
leave room for its `[K] ` prefix. The title is drawn into the top border with the
page counter. A title is never cut: where the border cannot hold the header, the
page draws a plain border and puts the whole header in its first row instead,
and that row is charged to the capacity like any other. A screen whose title
will not fit a 40-column border names itself more briefly rather than spend a
body row on a header -- the Hall of Fame's `Completed careers` becomes `Fame:
Completed careers` -- measured against the widest counter it can reach, so the
title cannot change under the caller. The deck's hull/fuel/hold gauges and the
fight screen's opponent and hull gauges are back with the frame, and give way to
plain numbers -- never the other way round -- on a row too narrow for both.

**Both bundled games require a terminal of at least 40 columns by 12 rows, and
have exactly one layout above it (issue #495).** A launch on anything smaller is
refused, by name and with the size the terminal reported, before any save or
world is opened. The doors used to carry a second, stripped presentation for
terminals down to 20 columns, and that is what flattened them: art, gauges and
rules were dropped so content would fit a caller nobody dials in from, and the
stripped result was then what an 80-column caller saw as well. A narrow case may
never set the ceiling again -- which is why there is no second layout to leak
upward, and why a screen that will not fit 40 columns is redesigned rather than
given a compact variant. Nothing in the host has ever supported below 40 either:
the prose editor already clamps to 40, and the menu layout's only thresholds are
72 and 120 for *wide* screens.

Door presentation is reviewed by looking at rendered screens. `scripts/
door_gallery.py <door>` drives every screen in a subprocess at 80, 64 and 40
columns in each display preset and writes an HTML page through the website's own
ANSI emulator; a change to a screen comes with that page. The test suite can only
assert that a screen fits -- never that it looks like anything -- which is how an
entire visual design was lost with every slice passing review.

### The Voidrunner presentation contract (issue #493)

Voidrunner is the showcase for what a NetBBS door can look like, and after the
#310 overhaul it was a sequence of grey text walls behind hotkeys. The
presentation half of that overhaul is this contract. It is normative for the
door; Retro Trivia is the floor it must clear, not the target.

**Color reaches the body.** Every body row of every paged screen carries
styling. The single mechanism that deleted the game's color was the shared
paginator: `wrapped_group` wrapped each row through an ANSI-stripping helper
before it was printed, so the frame was the only styled thing a page could
have. It now wraps styled text, carrying the active color across a break, and
`draw_page` colors by role anything that still reaches it plain -- a screen
that builds its own rows cannot opt out. This is asserted, at 80 and at 40
columns, on the deck, market, yard, record, board, chart, crew, display,
customs and combat screens.

**Nine roles, not nine colors.** What a token *is* decides its color.
`hull` `#7fa3bf` frames, section headers and station names; `deep` `#2f3844`
frame shadow, gauge tracks and separators; `plasma` `#ff5abe` the brand, the
rank and the cursor; `gold` `#ffc83c` hotkeys and credits and nothing else;
`ink` `#e8f0ff` values -- the thing the caller reads off the row; `slate`
`#b39b7d` labels, hints and units; and `mint` `#6cf2a0` / `amber` `#ffb347` /
`alarm` `#ff5c6c` for good, caution and danger on gauges and severity glyphs.
A hotkey is always gold and bold; a value is always ink; a label is always
slate; chrome is never the color of content; one accent carries the eye per
screen. That the hotkey, label, value and frame colors differ, and that all
four appear on a drawn screen, is asserted rather than eyeballed.

The label role is deliberately the one drawn from outside the cockpit's own hue
(issue #519). It was `#7f8fae` -- a desaturated *blue* on a blue screen -- so a
label read as the same color one shade down rather than as a different kind of
thing: measured on the command deck, `deep`, `hull`, `ink` and that label color
were 78.7% of visible characters in one band. Warm rather than neutral because a
label has to separate, not merely differ, and desaturated so it cannot be
mistaken for `gold` or `amber`, which mean money and hotkeys. War Dialer's own
label role made the same mistake in green and was moved the same way.

Chrome recedes (issue #519, the second half). Moving the label role changed
8-11% of the screen; the remaining mass was the frame. `hull` was `#5fd7ff`, a
saturated cyan, and `deep` was `#1d3b57`, a saturated dark blue; together they
were 49.7% of the command deck's visible characters, 85% and 99% of them
box-drawing. Chrome that bright competes with the values drawn on it. `hull`
is now steel -- the same hue at half the saturation and a step down in
brightness -- and `deep` is a near-neutral dark, so a gauge's filled half is
the only colored thing on its row. Both remain distinct from every content
role; the sixteen-color preset reads them as dark cyan and bright black. The
before/after is judged from `scripts/door_gallery.py`, not from the hex; the
command deck and the market, before above and after below, are kept as
`docs/images/door-chrome-519-voidrunner-deck.png` and
`door-chrome-519-voidrunner-market.png`.

A preview count inside a label is a *value*, not part of the label: the service
menu writes `Board: 4 offers` with the count in `ink` between two `slate`
fragments, because the count is what a caller opens the menu to read. Without
the colon the label read as a sentence -- subject "Board 4", verb "offers" --
and a caller asked what the numbers meant (issue #518). Counts agree in number
with their noun.

**A value is a value whether or not it is a number** (issue #532). The rule
above is the shape of a recurring mistake, and the count in a menu label was
only its first instance. `style_body_line` assigns color by token *shape* --
digits, credit figures, gauges, severity words, `[K]` -- and anything it does
not recognise falls to the prose role. So a value that happens to be a word had
nothing to match and came out the color of its own label: `HULL ... 60/60
Intact`, `Concord Navy ... +0 Neutral Not joined`, `Crew none`. Two style lists
made it worse by declaring those columns `label` outright. A column that holds
what the caller reads off the row is a value column, whatever its cells look
like; absent severity means *value*, not *label*, and only the bar carrying that
same absence stays chrome.

**A table's headings are structure, not labels.** They take the `heading` tone,
which is `hull` -- the role that already means section headers -- so a table
announces itself as one instead of being the color of the sentence above it and
the footnote below it.

**The action bar wears the roles of the menu it mirrors.** A footer is a row of
`[K] Label` pairs, not a sentence with keys in it, so `out_prompt` styles it the
way `key_label` styles the service menu: gold key, value label. It is done in
`out_prompt` rather than at the thirty call sites that print a prompt, and a
prompt that styles itself is left alone -- the same rule a composed body row
follows. Before this, the action bar was the only row on a Voidrunner screen
carrying no palette role at all, and it is the row a caller has to read to know
what to press.

**A screen of figures is not a screen of sentences.** The Trading Ledger was
fifteen prose rows with `HOLD -`, `TRAVEL -` and `LOCAL MARKET -` as headings
wearing a hyphen; 73% of its characters were the prose role and 0.4% were
values. `section` and `table` exist for exactly this, and every fact it carried
survived the change of shape. Where a table's rows each hold a single figure,
no column may be `optional`: dropping one leaves a row with nothing on it, which
is what a first cut of that table did to `Cargo lost or surrendered` at forty
columns. With nothing droppable the table stacks instead, and a stacked row
still carries its figure.

**A menu is built, not pattern-matched.** The Hall of Fame wrote its view
switcher as the sentence `Views: [1] Wealth, ... [5] Completed careers.`, and
`Completed` matched the good-tone severity pattern, so one view name rendered
green for no reason. A component's decision beats a pattern's guess -- the rule
that settled the preview counts -- so the switcher is a `menu_grid` under a
`section` rule.

**A screen replaces the one before it** (issue #516). A door owns the terminal
for its whole session, so every paged screen clears and repaints rather than
printing underneath its predecessor: a session was one long scroll, and a
caller's terminal history filled with superseded copies of the command deck.
The clear lives in `draw_page`, the single path every paged screen already goes
through, not sprinkled per screen -- a per-screen rule applied to most screens
and not the rest is how the color was lost one slice at a time. It is
unconditional rather than following the host's `redraw_in_place` preference,
which is opt-in because clearing a *menu* costs scrollback a caller may still
want; a door's own screens are not that, and War Dialer has always cleared.
The hand-off into a door does follow the preference: the host clears before
launching for a caller who redraws in place, because the door is a new screen
and its title card otherwise arrived under the remains of the door picker
(issue #648). A caller who scrolls keeps the picker in their scrollback.

What that replaces has to survive it. The launch banner is wiped by the first
deck the caller lands on, and everything it said is on that deck already -- the
station, the day and the credits in the status band, the tracked contract in the
alerts -- except a futures order that has come due, which was the one thing on
it a caller had to act on. That is an alert row now, where the contract says
something a caller must answer belongs.

**Truecolor is the design target**, degrading to 256, to 16 (`basic`), to
monochrome, to plain ASCII, in that order, each deliberate rather than
accidental. The only effect that truecolor buys outright is the title splash's
gradient rule, which degrades to a single role color rather than being
approximated.

**One glyph vocabulary, every glyph with an ASCII substitute.** `╭─╮ │ ╰─╯
├─┤` frames; `█░` gauges; `▁▂▃▄▅▆▇█` sparklines; `⟦ ⟧` chips; `◈` credits;
`▲ ◆ ●` severity; `◤` the brand; `●○` crew pips; `→` a delta. A screen asks for
one by role -- `glyph("danger")` -- rather than typing the character, so the
`plain` preset is a designed rendering and a new glyph cannot arrive without
its substitute. No Unicode from the vocabulary may reach a `plain` terminal.

**A component library, local to the door.** Voidrunner ships as a single
self-contained file a SysOp can point straight at, so this is the game's own
vocabulary rather than something shared: `gauge`, `sparkline`, `chip`, `badge`,
`table`, `menu_grid`, `alert`, `status_band`, `portrait`, plus `section` for a
named rule across the page frame. Every screen is built from them, which is what
makes the contract enforceable -- a gauge is the same gauge on the deck, in the
yard and in a fight, and a later slice cannot flatten one screen without
flattening all of them.

**Tables are tables.** A column starts on the same display column on every row
(right-aligned columns end on one), asserted by measuring the rendered rows.
When a table will not fit, it drops the columns the screen has named as
droppable, worst first -- so a narrow table can carry fewer facts than a wide
one, and a screen naming a column droppable is saying that figure is available
elsewhere (the market's depth figures are on the commodity's own trade screen).
When dropping all of them is still not enough it *stacks* -- each record's first
column on a row of its own, the rest aligned and indented beneath, and on a
further row where one is not wide enough -- rather than overflowing and wrapping
into rubble. Stacking never drops anything: every column that survived the
dropping step is on the record somewhere, so the narrow caller reads those facts
in two or three rows where the wide one reads them in one. The two steps are
different promises, and a screen chooses between them by what it marks optional:
mark a column optional only when its figure is a keypress away (the market's
depth is on the commodity's trade screen; the chart's sector and economy are on
the star map's Info; the yard's post-refit hold is on the commissioning
preview), and leave it un-optional when it is not -- the Hall of Fame's rank,
job and run counts are on no other view, so that table stacks instead. A stacked record stays one paginator entry and moves between pages
whole. A table's column headings are repeated at the top of every later page
that carries one of its rows, and only there.

**Layout.** Three menu columns at 72 or more usable columns, two at 52 or more,
one below. Numbers right-aligned. One blank row between logical groups. The
action bar outside the frame, where the cursor waits.

**Motion is in, and it replaces the old "no animation delays" rule.** Reveals,
gauge drains, counter ticks and rank climbs are permitted under three
conditions, none of them negotiable: any keypress ends the effect immediately;
no effect may delay a commit or hold up input; and every effect is absent from
the presets that exist because a caller wants less -- `fast`, `mono` and
`plain`. A reveal belongs to *arriving* at a screen, never to redrawing one: a
key that changed nothing redraws the same page and costs the caller nothing. An
effect with no live terminal on the other end -- a scripted session, a screen
drawn before stdin is open -- is skipped rather than slept through, because an
animation nobody is watching is only a delay. `fast` is a display preset beside
`auto`: the same palette with every effect off.

**The launch splash is motion too.** Once per launch, after the career's
preset is applied and before the title card, the door plays a full-screen
splash of about two and a half seconds: a parallax starfield, a ringed planet
drawn two pixels to a cell in half blocks, a ship crossing on a cooling plasma
trail, and the wordmark resolving in over it. It follows every rule above, and
it is the one effect that takes the key that ends it: whatever follows is a new
screen the caller has not seen yet, so a key pressed to skip a title card must
not act on it. Keys typed before the splash appears are left for the game, and
a launch that has already shown a refusal or a recovery screen skips it. It
draws only the cells that change between frames, never the bottom-right cell,
and composes a complete picture at every size the door accepts (the large face
from 72x20, a 39-column compact one below). At sixteen colors each color is
read by hue family rather than nearest distance, which kept the planet magenta
instead of grey.

Voidrunner offers saved display presets from station Display Options: full palette
using the existing terminal color depth, the same palette with motion off
(`fast`), basic 16-color, monochrome Unicode, and plain text with ASCII artwork.
Monochrome/plain suppress ANSI styling; plain maps the whole glyph vocabulary to
equal-width ASCII while retaining Unicode pilot text and input. This is an
artwork fallback, not a change to the UTF-8 door transport. Each preset previews
itself on the Display Options screen: the sample beside a preset's name is drawn
the way that preset would draw it, so the choice is made by looking rather than
by reading an adjective. A chosen preset checkpoints before acknowledgement;
browsing and reselecting the current preset write nothing. The validated additive
preference defaults to full palette for older careers and survives retirement.
Apply it after loading a valid career and before its normal title/welcome output;
recovery uses the default presentation until a valid career is available. Motion
is on in `auto` and `basic` and off in the other three, under the conditions in
the Voidrunner presentation contract above.

Economy safeguards (issue #310): Blackwake standing from trade follows each new
250-credit high-water milestone in cumulative contraband sales minus purchases
and new futures outlay/refunds. The step is 250 because the non-Haven demand
pool is capped at 48 units, so a wider step made trade alone need about a
hundred round trips to reach the Cartel. Buying and same-station recycling do
not grant standing; splitting transactions cannot reset milestones. Existing
standing is retained and the new ledger starts at zero for old careers (old
cargo has no recorded acquisition cost). Because the saved counter is standing
already granted rather than progress, each career records the step it was
awarded under and loading re-expresses the count in the current step; a career
that earned two points per 500 credits loads as four points per 250, so
changing the step never mints retroactive standing. Combat/faction rewards
remain separate.

The trading ledger records cargo acquisition costs from this version onward.
Market purchases and collected futures carry their actual paid cost, including
brokerage; older cargo has unknown cost and is never assigned a fabricated basis.
Disposals consume older unknown cargo first, then recorded purchase lots in order.
Partial lots retain integer cost remainders so disposing of a complete lot always
accounts for its exact cost. Market sales and delivery contracts show separate
realized margins for costed cargo and receipts whose acquisition cost is unknown.
Mixed delivery payments are allocated by quantity; these are trading margins,
not total career profit. Lost cargo basis, cancelled-order fees, actual fuel
purchases and paid crew wages are reported separately. Unrecorded historical
activity, repairs, fines, hire costs and other career income are excluded.
The ledger resets with a new career, checkpoints with each affected action and
is read-only when viewed.

Docked checkpoints remember visible local buy/sell quotes with their observation
day; browsing never refreshes remote data. Earlier discoveries have no inferred
price history. Trader data-burst encounters also retain the actual quote they
reveal, checkpointed with the encounter result without changing RNG call order.
Each commodity keeps its last observed quote, including contraband
previously carried through a station that does not openly sell it. The ledger's
route estimator compares a new purchase or existing hold cargo with a remembered
destination sale quote. It displays the route, quote age, acquisition basis,
replacement fuel at 6 credits/unit, crew wages, cash shortfall and estimated
margin. Unknown acquisition costs suppress a total-profit claim. Refuelling is
budgeted at intermediate stations where necessary; a leg exceeding tank capacity
is infeasible. Unknown intermediate systems retain unknown names/threats. Prices,
encounters, detours, repairs, other income and contract deliveries can change the
outcome; estimates are advisory and never buy cargo or launch travel.

Route parameters use an explicit draft editor: destination, commodity, quantity
and cargo source are edited together, then applied to the read-only estimate.
Rejected combinations retain the draft; Back discards edits. Applying a route
draft never purchases cargo, launches travel or writes a career. The standalone
door implements this synchronous editor contract locally, because the host's
async resource editor requires Session and DatabaseLane objects unavailable in
the subprocess. Remembered station IDs must belong to the saved chart; inconsistent
observations are rejected at the recovery boundary before names are rendered.


Spot-market depth (issue #310): each pilot's station markets have finite stock
and buying demand. A station carries up to 96 units of each locally produced
commodity and 48 of other goods; it buys up to 96 units of each locally demanded
commodity and 48 of other goods. Each pool replenishes by 6 or 3 units per game
day respectively, capped at its maximum. Only jumps advance that clock; browsing,
restarting and splitting trades do not replenish either pool. Sales add goods to
stock up to its ceiling, but buying does not restore the station's spent buying
demand. Existing price drift, spread and economy-event rules still apply.

Limits are per career, matching the single-player economy. Old careers begin
with full pools; no historical transactions are reconstructed. State is additive,
bounded to the existing galaxy and commodities, and computed without new RNG
calls. Read-only quotes do not create or advance records. Spot purchases and
sales validate limits before changing credits, cargo, accounting or prices, and
persist pool changes with the completed trade. Local quotes show stock, demand
and daily replenishment. Remembered quantities are timestamped observations,
never live remote availability; a route cannot promise sale of a load exceeding
observed demand. First Flight must quote immediately available procurement.

Futures remain separately scheduled wholesale consignments with their existing
fee, term, issuing-station pickup and hold-space requirements. A new order
reserves its quantity from the issuing station's spot stock pool at signing and
applies the ordinary purchase price nudge (issue #401): an order larger than the
available stock is rejected before any change, cancellation returns the
reservation to that station's pool bounded by its ceiling, and settlement does
not consume stock again. Orders signed before this rule carry no reservation and
keep their terms, as do legacy remote-delivery orders. Unloading collected goods
still uses the destination's buying demand. Contract deliveries consume their
contracted cargo independently of spot demand.

New economy events affect a bounded region: an anchor station plus up to two
same-economy stations within two jumps. A deterministic selection from the
existing seed/day/event terms chooses the region without RNG calls or changes to
galaxy generation. The event-creation function retains its existing draw sequence;
later gameplay can differ because the affected market state changes. An absent
selected industry falls back to the current station economy and its first listed
trade commodity, retaining that event's original draw count and duration. Existing saved events without
regional IDs retain their original economy-wide scope until they finish. Price
levels, event frequency and duration keep their existing rules; stock/demand
pools do not refill merely because a price event starts or is viewed.

The ledger's Opportunities screen combines public regional news with up to six
ranked outbound spot-trade candidates derived from the pilot's remembered prices.
News gives named affected stations, coordinates, hops, remaining time and an
explicit unknown-danger label where appropriate; this public bulletin does not
chart stations or create market observations. A boom suggests bringing that
commodity; a crash suggests investigating cheaper procurement. These are leads,
not promises of available stock or profitable resale.

Trade candidates use current local stock/price/hold space, remembered destination
demand when available, fuel and wages, and a cash budget sufficient to reach the
sale. They exclude infeasible routes and loads consumed by active deliveries,
rank positive estimated margins per outbound jump, and open the existing route
estimate/draft without buying or travelling. Quote age, unobserved buying capacity,
legality and excluded return travel remain visible; no live remote prices are
queried. The board is read-only and paginated. Human play remains necessary to
validate whether these opportunities encourage satisfying route variety.

The engineer costs 200 credits to hire and 2 per jump. Fuel savings follow the
paid-service progression defined above; one-unit jumps cannot benefit, and
hiring does not refund old hire costs. Because most adjacent jumps cost one unit,
fuel alone was worth under a credit per jump (issue #409), so the engineer also
cuts yard repairs from 4 credits per hull point to 3 (Recruit and Seasoned) and
2 (Veteran and Ace); the yard and crew screens show the current rate.

New futures orders lock goods for pickup at their issuing station after maturity.
Up to eight orders may be active; insufficient hold space leaves a ready order
waiting. Orders can be cancelled remotely for their recorded goods principal;
the 8% brokerage fee (rounded up per unit, at least one credit) is never refunded.
Splitting orders cannot reduce that per-unit fee. Purchase screens show quantity,
term, pickup station, principal, fee and the station stock remaining after the
order before signing, with Back writing nothing.
Mature goods settle on arrival or station entry before mission completion checks.
Every saved order carries its pickup and principal metadata; a document without
it is refused with the rest of its schema-1 career rather than settled early
under terms its pilot did not agree to.

Voidrunner permits one active session per pilot within a save directory. A
nonblocking OS file lock is held from before loading through the final checkpoint;
a second launch reports that the career is already in use and changes no career
or score. Process exit, including forced termination, releases the lock. Distinct
pilots may play concurrently. Lock files remain in place and must not be deleted
while the service is running. This requires a local filesystem with working OS
locks and atomic replacement; cross-host shared directories are not supported.

The resolved save directory is the installation namespace, and it belongs to
one node: `<db path>.doors/voidrunner/`, beside War Dialer's world, unless the
SysOp sets `VOIDRUNNER_SAVE_DIR`. The node resolves it and passes it to every
launch as an absolute path; the door's own `~/.netbbs/voidrunner_saves` fallback
is for standalone play only. That home-directory path was the node default until
issue #648, and it made every node one OS account ran share careers by user id.
On its first start after the change, a node with no override whose own record
names the legacy directory copies it into its own under the legacy directory's
maintenance gate, staged and renamed into place whole. The record is the evidence
the careers are this node's: a brand-new node under the same account has none, and
never adopts careers another node left there, since user ids do not transfer. A
target that already holds careers is never overwritten. It copies rather than moves, because a second node
under the same account may still be reading it; each node then owns its copy. A
busy or unreadable legacy directory is left alone and the node keeps using it
until a later start copies it. Changing a node's display name does not change
career identity. See the door guide for the upgrade and override steps.

Hall of Fame records are retained independently at `scores/<user_id>.json`; only
the displayed ranking is limited to 20. A `leaderboard.json` from before those
records is imported into `scores/` once per save directory, under the maintenance
gate, and the old file is retained but no longer read at runtime. The import
belongs to the launch rather than to a career: a node whose only returning caller
holds a refused schema-1 save still imports its rankings, which is exactly when
they matter most. Scores outlive careers, so they survive the schema-2 cutoff
even though careers do not. Updates replace only that pilot's file using a flushed
private temporary file. The career save also retains the credit high-water mark
across retirement and temporary score-write failures, allowing a later checkpoint
to repair its score. The career save is authoritative for live gameplay state and
per-career achievements. The compatibility import of validated lifetime wealth
and retirement maxima described above is the sole exception: these retained totals
can raise the save's historical floors, and the retirement count affects a later
New Game+ starting bonus. Other score fields never replace current gameplay or
invent missing dossiers. Score writes remain optional projections; failure does
not erase totals already retained in the career save. Historical records already
discarded by older top-20 storage cannot be reconstructed. Node backup coverage and explicit external-directory restore
are specified in section 13.4 and the door guide.

Compatibility extension (issues #296/#297):

- A nullable, versioned profile preserves the original stdio API for existing
  registrations: no drop files, no adapter, raw UTF-8 through stdin/stdout.
  SysOps can explicitly remove a profile in the draft editor without recreating
  the registration; executable/argv and game data are retained. Native socket
  profiles require DOOR32 descriptor metadata.
- The launch metadata file (`door_info.json`) is itself versioned, by a
  `door_api` integer, and grows additively (issue #469): a reader treats any
  absent field as unknown, and a door may refuse a version it does not
  understand rather than probing for fields. Removing a profile restores the
  stdio API but does not pin the metadata to an older version — the file is a
  property of the platform, not of the profile. It never carries a credential,
  an email address, a user level or a network address.
- Which door a caller is in is presence, not catalogue data, and is scoped to
  the viewer (issue #470). It is held per *session*, not per account: both Who
  screens render a row per session, and the SysOp one acts on the row
  selected, so an idle connection must never claim the door its sibling is
  playing. A caller-facing screen names a door only when that viewer could
  currently open it — the same `min_play_level` gate the door picker applies —
  so Who can never advertise a door a caller is not allowed to see. A door
  deleted since, or one whose registration no longer matches the activity
  recorded, is omitted rather than named; door ids are reusable, so identity
  is checked, not just the number. A SysOp screen names everything, being
  SysOp-only already. Remote presence carries no door: a linked node reports
  who is online, not what they are doing.
- A door may post to boards and speak in chat channels a SysOp allowlists for
  it (issue #520, the outbound half of #470), and to nothing else. It posts under a **label**, not
  an account: `author_user_id` and `author_fingerprint` are NULL and
  `author_label` alone carries the identity, which is the shape a Link-carried
  post has always had. An account was rejected as the answer -- `users`
  requires at least one credential by CHECK constraint, so a credential-less
  service row could not exist without weakening it -- and with no account
  there is nothing to exclude from login, listings, mail or moderation, and
  the infrastructure level band sketched under issue #63 is not needed for
  this. The label ends in a reserved suffix, is unique across accounts and
  doors, and is checked at the moment the hook is switched on: it is shaped
  like a handle because it federates as `local_user_id`, and chat resolves a
  stored author by username where boards resolve by id, so a collision would
  let a door speak in a real account's presentation. The SysOp's allowlist is
  the only gate on this path; level is deliberately not a second one, because
  two gates can disagree where only one is visible to the SysOp. Transport is
  a file drop rather than a socket, chosen so a DOS door can use it at all,
  and a refusal is always returned to the door and never queued -- a held post
  would publish after the allowlist was revoked. Posting is rate-limited per
  door and audit-logged against the SysOp whose authority it runs on; if that
  account is deleted the hook lapses rather than posting unattributably, and
  another SysOp vouches for it without disturbing its identity or allowlist.
  An outcome is written where the door can still read it after the run: the
  working directory is gone by then, so a result left there would make the
  promise that a refusal is always visible untrue in practice. Those receipts
  are node state and travel with the node (§13.4, issue #556): a backup
  captures them before its database snapshot, and a restore pairs the receipts
  it carries with the database generation they describe. They remain a bounded,
  best-effort record rather than a publication ledger, so a door may not read
  exactly-once delivery out of them. Processing is at-most-once, though: a
  drain claims each request by renaming it out of the request pattern before
  reading it, so no later drain can see it again even if its removal fails --
  the property that makes draining during a session safe. A running door's
  requests are picked up every two seconds, a few at a time, and once more at
  exit; only the drain at exit refuses what is left over. A SysOp's test
  launch is drained too, but answered with a `rehearsal` verdict: nothing is
  posted, debited or audit-logged. A chat line (slice 2) is one line,
  control characters stripped, `kind="message"` only, recorded exactly as a
  caller's line is -- scrollback, search, Link queue -- and delivered live by
  the session that launched the door, since only it holds the chat hub. It
  has its own hourly ceiling (30 by default) so neither budget spends the
  other, and is not audit-logged per line: lines scroll away and a busy door
  would bury the moderation log, while the label names the speaker. Three
  rules keep it civil. An MRC-bridged channel is never a target, because the
  bridge would present the door to the hub as a local caller. A Linked
  channel needs the SysOp's explicit confirmation, because chat has no
  retraction and the line reaches every peer. And a channel's moderators can
  `/mute` a door there, timed like a caller's mute, by suspending its
  allowlist entry -- a door has no account for `channel_restrictions` to name.
  A door's line renders muted and marked; peers key on the reserved `.door`
  suffix, so a newer peer styles a door's line too and an older one shows it
  plain. Reads of any
  kind remain out of scope, and the hook is available to locally-launched
  doors only -- a remote registration shares no filesystem, and a DOS guest
  cannot read the launch metadata that names the drop directory.
  Profiles add persistent installation directories,
  disposable node directories, exact CRLF classic drop files, native stdio,
  controlling PTYs, private inherited DOOR32 sockets, DOSBox-X COM1 sockets,
  and allowlisted outbound RLogin services. Never pass the caller's socket.
- A door's stream is transcoded to and from the caller's character set (§3.2):
  a CP437 door reaches a CP437 terminal unchanged, and is transcoded for UTF-8
  and ASCII ones; a UTF-8 door is mapped for CP437 and ASCII. Browser door
  mode uses bounded base64 output frames and stream-scoped raw key events;
  stale door input cannot become menu actions. Resize stays out of band;
  fixed geometry is restored to browser-fit geometry on exit. This does not
  add browser Zmodem support. Explicit raw profiles are native-terminal only.
- Each POSIX launch owns a process group. An internal, freshly exec'd Python
  helper applies limits and the optional controlling terminal, then execs the
  operator-selected argv; no threaded-process `preexec_fn`. Cancellation
  during spawn or cleanup retains ownership. Descendants are terminated on
  every exit path, even when their leader has already exited. Leader exit
  does not discard buffered terminal output: it drains to EOF while still
  respecting the session watchdog, caller disconnect and shutdown.
- Legacy installations default to one active session. Cross-process advisory
  node leases are keyed by resolved installation directory; higher limits
  require explicit SysOp certification of the game's locking. Persistent
  scores are owned by the door, not a new NetBBS database capability.
- DOSBox-X is external software. NetBBS generates narrow C: installation and
  D: node mounts, headless configuration, transparent inherited COM1 and
  secure mode after mounting. Optional FOSSIL software is operator supplied.
  Runtime diagnostics are bounded and SysOp-only; emulator stderr is never
  relayed to callers. DOS program status is checked separately from emulator
  status, including LORD's normal return code 255.
- The VM adapter (issue #474) runs a foreign-platform native door in a
  per-caller qemu guest the SysOp builds. NetBBS owns the command line: no
  network, display or host devices, one virtio console on the door's
  socketpair, and 9p exports of exactly the installation and node
  directories. The guest reports its door's status through `exit.status`;
  a graceful stop is an ACPI power-button press over a private QMP
  socketpair before the process group is signalled.
- RLogin requires a fixed allowlist and caller-visible service identity.
  Loopback SSH/TLS tunnels are the default; direct plaintext requires an
  explicit insecure-operation acknowledgement. Provider identity templates
  may read a private operator-created credential file. No caller chooses a
  destination and no privileged source port is requested.
- A provider with its own protocol gets its own adapter, never a creatively
  filled RLogin template (issue #565). BBSLink is the first: an HTTP token
  and authorisation step, then Telnet, both plaintext with no tunnel route,
  so every non-loopback destination needs the insecure acknowledgement.
  Both fixed destinations must be allowlisted, the service name is shown as
  for RLogin, and the three provider codes live only in a private
  operator-created file. The caller's user number and the door code are the
  only caller-related values sent. The provider binds the Telnet session to
  the authorisation by source address, so a node pins one resolved address
  per launch and serialises handshakes per provider host.
- A SysOp draft editor provides packaged setup templates, JSON import,
  preflight and an explicit test launch. Back saves nothing; Test may modify
  the game's persistent data. No game, driver, emulator, tunnel or host
  package is automatically downloaded or installed.

The bounded verification matrix and exact manual setup instructions live in
[the door guide](NetBBS-door-guide.md). A template is not a certification of
arbitrary game versions, platforms, or multiplayer operation. Same-user
native code can read the service account's files, including keys and database;
resource limits are not filesystem/network isolation. DOS mounts improve the
practical boundary but cannot protect against emulator vulnerabilities. A VM
door is isolated from the host except for its two exports, but that boundary
is the SysOp's qemu build and guest image, not something NetBBS certifies.
Optional external runner argv is operator-managed and does not imply a
privileged or automatically provisioned containment environment.

Still outside the supported scope:

- Wine/Win32, Linux ELF emulation on NetBSD, DOSEMU, other BBSes' private
  scripting APIs, graphical/VGA scraping, RIP, sound or IPX forwarding;
- universal legacy compatibility, untrusted native binaries or caller uploads;
- any door capability beyond the raw-terminal-I/O metadata handshake
  above — deliberately minimal by design, add only what a real door
  actually needs;
- UI-only conversation/message-threading refinements — an item from
  this phase's original planning stub, never folded into #172's own
  scope and not otherwise picked up; still open, unscoped.

`RLIMIT_CPU`/`RLIMIT_AS`/`RLIMIT_NPROC` are set by the internal launcher
before the real program's exec (`src/netbbs/doors/launcher.py`), matching the locked design's
CPU/memory/process-count ceilings; the wall-time watchdog, crash
reporting, and cleanup-on-disconnect each have a real regression test
(`tests/test_doors_runtime.py`). Not yet independently verified: a
dedicated adversarial test that actually exercises a door hitting the
CPU/memory/process-count ceilings themselves (as opposed to the
watchdog's own wall-time path) — those three `setrlimit` calls are
implemented but currently rely on the OS enforcing them correctly, not
on a test proving it. What the CPU ceiling does when reached was observed
directly on NetBSD (issues #509 and #585, not through a test): the launcher
sets the soft and hard `RLIMIT_CPU` equal, so the kernel delivers `SIGKILL`
with no preceding `SIGXCPU`, and a door cannot checkpoint at the ceiling.
That is the documented contract (door guide, "CPU seconds"), chosen over a
soft-below-hard split: an unprivileged process cannot raise the hard limit
above the one it inherited, so the headroom may not exist at all under a
login class that pins `cputime`; the kernel re-sends `SIGXCPU` about once a
second after the soft limit, which a door handling it badly spins on; and a
split either moves the effective ceiling above what the SysOp typed or
quietly shortens every existing profile. `0` is the supported answer for a
door which should not be cut that way, with the limit `0` has: it raises
the door to the hard limit the service inherited, so under a login class or
unit file which pins `cputime` the door is still killed, without warning,
at that inherited value. Making a door truly uncuttable means removing the
service's own hard limit as well, which is a host decision outside NetBBS.
This design explicitly does not
attempt filesystem/network isolation regardless.

---

## 16. Decisions and extension references

GitHub issues are authoritative and may evolve beyond this summary.

### Issue #11 — canonical Link format

§7.2/§7.3/§7.4 now state the complete rule: canonical byte encoding (sorted
keys, recursive NFC, compact separators), the safe-integer bound, duplicate-
key wire rejection, omitted-versus-null field semantics, mandatory
object-type domain separation, event-identity distinctness (a nonce for
immutable creation events; `previous_event_id` chains, never `created_at`,
for per-object chains), `(home_node_fingerprint, local_user_id)` as a
node-vouched author's globally-scoped identity, and golden test vectors
(`tests/fixtures/link_canonical_vectors.json`).

Still open: this specification and its vectors exist only as this
codebase's own Python implementation plus one fixture file. No independent,
non-Python implementation has yet exercised the vectors to prove real
cross-language interoperability, and the rule is not yet published as an
external protocol document outside this repository. Closing that gap is
implementation/publication follow-up, not a further design decision.

### Issue #56 — unread, follows, activity, and search

§6.6 now states the complete design: cursor-based read/unread state for
boards, file areas, and channels (mail already had this); replies/mentions
derived from existing `parent_post_id`/message-body fields with no new
schema; a follow/favourite table independent of channel membership and node
carry; a `[N]ew scan` activity surface covering every accessible resource,
not only followed ones, with a direct jump to the first unread item; local
FTS5-backed search scoped to this node's own carried content, explicitly
never broadcast over Link; and a zero-backfill migration story (existing
users' read cursors start empty; first post-upgrade visit sets the
baseline).

Implemented: the read-cursor table (`netbbs.activity`), the follow table, and
the `[N]ew scan` main-menu screen, wired into board/file-area viewing and
channel scrollback replay. Verified against a real Telnet session, not just
scripted tests.

Also implemented: local FTS5-backed search (`netbbs.search`) over board
posts, files, and channel scrollback, synced from every write path, gated by
the exact same visibility rules browsing already enforces, and surfaced as a
new `[/] Find` main-menu entry that jumps straight to a selected hit. FTS5
availability, this round's stated blocker, was resolved by tracing pkgsrc's
actual build chain rather than empirical access to a NetBSD box: `lang/
python312` buildlinks against `databases/sqlite3`, whose own Makefile passes
`--fts5` unconditionally, so the target Python build should always have it —
and a build that doesn't fails the migration loudly rather than silently
disabling search.

Issue #56 is fully implemented; all four §6.6 subsections have shipped.

### Issue #72 — node-local arrival order for unread state — closed

§6.6's "Node-local arrival order for carried content" subsection now
states the complete design: `user_read_cursors.last_seen_arrival_id`,
sourced from `posts`/`files`' own rowid rather than authored
`created_at`, with existing cursors backfilled on upgrade. The one
accepted, documented scope boundary: jump-to-first-unread still uses
the `created_at`-based cursor and may not navigate precisely to an
out-of-order arrival, even though unread counting now correctly flags
it.

### Issue #78 — decompose LinkNode protocol state — closed

The engineering record's "LinkNode internal state organization" entry
(§9) is the design pass this issue asked for: which of `LinkNode`'s
eleven flat fields belong together (`PeerDirectory`, `BoardEventState`,
`BoardLifecycleState`, `RelayState`), and which stay directly on the
façade (`identity`, `known_event_ids`, `events`, as the shared
substrate every object type uses). Every external consumer
(`netbbs.link.store`/`.sync`/`.transport`/`.relay_selection`,
`netbbs.net.admin_flow`, and their tests) still reads the old flat
attribute names unchanged, via backward-compatible properties over the
same live dicts -- zero test changes, zero wire/serialized-shape
changes. A future state family (inventory/pull, linked-channel
lifecycle) should follow the same shape: its own small dataclass with
narrow methods, not a further flat field.

### Issue #80 — end-to-end regression tests for cross-subsystem Link orchestration — closed

§14.1's "Cross-subsystem end-to-end scenarios" subsection now states
the complete design: `tests/test_link_end_to_end.py`, a real-transport,
real-domain-read-path vertical slice per implemented Link product
surface. It now covers linked boards, linked channels, remote file
catalogue/fetch, and Link mail, with restart and duplicate-delivery
coverage where the surface has persisted state. The mail vertical also
covers a dead-letter -> replay -> real-redelivery cycle end to end. Confirmed the consolidated
mail scenario (and its restart variant) would fail on the pre-fix
issue #69 implementation by temporarily reverting the fix and observing
both fail, then restoring it. Future Link vertical slices extend this
file before being considered complete.

### Issue #81 — supported platform tiers — closed

§2.1's "Platform support tiers" subsection now states the complete
policy: NetBSD (Tier 1, primary, with dependencies preferably supplied
by pkgsrc), mainstream Linux/systemd
(Tier 2, supported), other POSIX systems (Tier 3, best-effort), and
Windows (development-only, no production-semantics promise). Auditing
every existing `sys.platform`/`os.name` branch (`netbbs.net.
local_terminal`, `netbbs.backup._process_is_running`,
`netbbs.__main__`'s signal-handler setup) found the codebase already
drew exactly this line in practice, each in its own narrow, already-
isolated function — this closes the gap between that existing practice
and an explicit, written contributor-facing policy, rather than
requiring code changes.

### Issue #82 — operator-ready installation and release path — closed

[SysOp handbook](NetBBS-SysOp-Handbook.md) is the complete operator lifecycle:
install (a real, tested non-editable wheel build/install with no
source-checkout dependency, now sourced only from official GitHub
releases), first-SysOp bootstrap via the existing `netbbs.admin`
CLI, running under systemd/rc.d (`examples/netbbs.service`/`netbbs.rc`,
the rc.d script since confirmed working on real NetBSD hardware,
including its `LD_LIBRARY_PATH` handling for the pkgsrc-vs-base OpenSSL
runtime-linking gap documented in the worklog §10), persistent
state paths, backup/restore (linking the existing disaster-recovery
drill), upgrading, version/schema compatibility, and uninstalling
without losing data. `python -m netbbs --version` (issue #82) prints
the release version and expected schema number together. Installing a
release from the console arrived later (§6.7, issue #731); manually
installing an official GitHub-release wheel into the node's virtual
environment remains supported.

### Issue #74 — FTS index integrity checks and rebuild tooling — closed

§6.6's "Integrity checking and rebuild" subsection now states the
complete design: `netbbs.search.check_index_integrity`/`rebuild_indexes`,
a standalone `python -m netbbs.search check|rebuild` command, and the
explicit decision that startup does not run this check automatically
(unlike `Database.check_integrity`). Reports drift by id only, never
indexed content, for all three FTS tables.

### Issue #60 — production operations — closed

Implemented across four slices, all merged: backup/restore (§13.4,
`netbbs.backup`, verified against a real running node including a
create-wipe-restore round trip and the live-lock restore refusal); outbound
work items/retry/dead-letter (§13.7, `netbbs.link.work_items`, scoped to
Link mail delivery and acknowledgement delivery specifically — not gossip or
relay maintenance, which don't fit the same shape — wired into
`netbbs.link.mail`/`netbbs.link.sync` and surfaced as an `[O]utbox` SysOp
screen); bounded quotas (§13.9); and startup integrity checking, diagnostic
log retention, protocol/database upgrade compatibility, and graceful Link
drain on shutdown (§13.11). Staged/validated restore (§13.10) shipped
separately as issue #75.

Rehearsing these controls against a real long-running node (not just their
original implementation tests) is tracked by the Phase 3 stabilization gate
above, not by this issue.

### Issue #85 — inventory/pull-based catch-up and multi-hop relay

§8.8 now states the complete design: a signed `InventoryRequest` bundle
(not a canonical event, matching `PeerListMessage`'s own precedent), a new
`POST {LINK_PATH_PREFIX}/inventory/{fingerprint}` route whose response
reuses the exact `push_events` raw-event-list wire shape, a responder-side
diff query unioning three sources (self-originated, locally-authored on any
carried board, and peer-received) so it is genuinely multi-hop, and a
nullable `board_id` column added to `link_events` to make that query cheap.
Bounded by the existing `_MAX_EVENTS_PER_REQUEST`/`max_carried_boards`
quotas, not new numbers.

**One necessary correctness fix, not zero protocol changes**, discovered
while implementing: `handle_events`'s board-scoped branches previously
required the wire-level sender to equal the content's own claimed
origin/author, which made a genuinely relayed event structurally
unverifiable (the relay is a different node than the author). Fixed by
resolving each branch's signing key against the content's own claimed
origin/author instead, gated on that fingerprint independently already
being a completed peer — preserving the same "never accept from a stranger"
property while correctly relocating which fingerprint it applies to. See
§8.8's own "real, worth-stating limitation" note for what this does and
does not enable.

Issue #94 subsequently widened responder enumeration to resources absent
from the request, so an empty inventory can discover a wholly novel board,
channel, or file-area catalogue through a carrier. The signed requester
authentication from issue #106 and destination/freshness/replay binding
from issue #124 are now part of that disclosure boundary; §8.8 is the
normative current request format.

Explicitly excludes retention/purging (issue #86, sequenced after this one)
and Link messages (already point-to-point by design, §10, untouched by the
`handle_events` fix above).

### Issue #86 — event/dedup retention

§8.9 now states the complete design: the chain-idempotency gap was in
`board_origin_transfer_offer`/`_accepted`, the only two board-scoped types
whose idempotency depended solely on the fast `known_event_ids` cache
rather than a self-heal check against their own authoritative state
(`pending_offer`/`board_lifecycle_head`) — fixed with the same self-heal
shape `key_transition`/`board_post_edit` already use. Tracing what else
depends on each object type's `link_events` row surviving (restart
reconstruction, and issue #85's own inventory diff) found only
`key_transition` genuinely redundant with an already-durable separate
source (`link_peers.transitions_json`); everything board-scoped stays
unbounded in this issue, stated explicitly rather than silently assumed
safe. `netbbs.link.store.purge_expired_key_transitions` purges on write
(90-day fixed window), the same shape `LinkDiagnosticLogHandler.emit`
already established for `link_diagnostic_log`.

### Issue #90 — tier-2 Link message routing scope — deferred

§10.6 now states the complete answer: distinct from the unrelated,
already-decided `tier2_personal_key` non-goal (§10.2, a hard architectural
blocker); this is a recipient-*reachability* question with no equivalent
blocker, just not built. Neither issue #85's relay nor issue #630's
identity introduction provides tier-2 reachability: the first carries
content, the second lets a receiver verify the author of carried content,
and Link mail still requires a completed hello with the recipient's node.
The relayed, self-certifying `HelloMessage` bundle exchange this entry
first named as the missing piece was built for #630, for verification only;
§10.6 lists what mail would need beyond it. Deliberately deferred rather
than scoped as active work: no architectural blocker, just not needed yet.

### Issue #87 — linked channels — closed

§9.6 states the complete design and is now fully implemented:
`channel_genesis`/`channel_message` event types mirroring `board_genesis`/
`board_post` minus the fields that don't apply (no edit chain — channel
messages have no local edit concept at all; no `default_min_write_level`/
`_moderated`/`_max_post_age_days` equivalents — `Channel` has none of those
settings to recommend). Origin succession is reused by reference (§9.4's
model applies unchanged, if ever needed) rather than a new
`channel_origin_transfer_offer`/`_accepted` pair built in this issue —
genesis, promotion, materialization, and message propagation are the
actual scope, matching what actually shipped. `netbbs.link.channels`
mirrors `netbbs.link.boards` closely; `ChannelEventState` (genesis only, no
edit-chain half) mirrors `BoardEventState`; `handle_events` gained
`channel_genesis`/`channel_message` branches with issue #85's multi-hop
verification model from day one (never had the older restriction to begin
with). Issue #85's inventory mechanism (`InventoryRequest.channels`,
`channel_event_diff`) and issue #86's restart reconstruction both extend
to channels the same way they already cover boards.

Carry/materialization follows §9.3's exact shape, with one real, stated
consequence of reusing the existing bounded scrollback rather than
inventing unbounded storage for channel content: a materialized linked
message is subject to the same trim a local one already is, and a
self-originated message trimmed before any sync pass pushes it is simply
never propagated — bounded and honestly scoped, not a silent surprise,
since the identical bound already governs every channel's own local
history today. `channel_messages.link_content_id` is a new column solving
a real gap found while implementing: unlike `posts.post_id`,
`channel_messages.id` is a plain autoincrement with no existing
content-addressed column to key idempotent materialization off of.

**Not done in this issue, closed by issue #91:** wiring `queue_channel_
message_if_linked` into `netbbs.net.chat_flow`'s live interactive send
path was deferred here (`chat_flow.py`'s message-send code had no existing
`link_context` threading at all, unlike `login_flow.py`'s board-post path)
and completed separately — see issue #91's own §16 entry.

### Issue #88 — board closure, moderator edits, tombstones — closed

§9.5 states the complete design and is now fully implemented: `board_closure`
(extends §9.4's own `board_lifecycle_head` chain, terminal), `board_post_
moderator_edit`/`board_post_tombstone` (both extend the same per-post chain
`board_post_edit` already does, origin-signed rather than author-signed, the
latter terminal too). No new authorization primitive — all three verify
against the board's current origin's signing key exactly like `board_origin_
transfer_offer` already does; the two post-scoped types rely on a local
`BoardPermission.EDIT`/`DELETE` check already made on the origin node, before
`netbbs.link.boards.queue_board_post_moderator_edit_if_linked`/`queue_board_
post_tombstone_if_linked` ever builds the event — never a new gossiped grant.

`posts.tombstoned_at` is a plain nullable `ALTER TABLE ADD COLUMN`, not a
`CHECK`-widening rebuild: an earlier migration (post-`root_post_id`) already
found and documented that `posts` cannot safely go through the usual
drop/rebuild pattern, since it is a live *self-referencing* FK parent
(`parent_post_id`/`root_post_id`/`edit_of_post_id` all reference `posts.
post_id`) — the additive-column path sidesteps that risk entirely rather
than re-testing it. `netbbs.boards.posts.tombstone_post` is a new local
function, not a repurposed `delete_post`: it inserts a further
content-addressed revision (placeholder subject/body, `tombstoned_at` set)
rather than removing the row, so the edit chain and any reply's
`parent_post_id` stay intact — `delete_post` itself is unchanged, still
reserved for a still-`'pending'` post's rejection.

**Live UI wiring, stated explicitly:** `board_closure` and `board_post_
moderator_edit` reach real interactive call sites this issue — a `[C]lose`
option on the board admin screen (`netbbs.net.admin_flow`, gated the same way
`[T]ransfer origin` already is: origin only, board not already closed), and
the existing `[E]dit` flow (`netbbs.net.board_flow._edit_existing_post`)
building a moderator-edit event instead of a self-authored one whenever
`edited_by` isn't the post's own author and this node is the board's origin.
`board_post_tombstone` did not have any existing "delete an approved,
already-published post" UI action to extend (the only existing `delete_post`
call site handles pending-post rejection, which never reaches an already-
`board_post`-queued row) — a new `[T]ombstone` option was added alongside
`[E]dit` for exactly this reason, gated on `BoardPermission.DELETE`.

### Issue #89 — remote file catalogue and chunk transfer — closed

§11 states the complete design and is now fully implemented: `file_area_
genesis`/`file_descriptor` mirror `board_genesis`/`board_post` for catalogue
discovery (no content, no edit chain, no origin succession — the same
deliberate deferrals issue #87 already set for channels); chunk transfer
(`netbbs.link.file_transfer`, `FileChunkRequest`/`FileChunkDescriptor`) is
genuinely new — a direct point-to-point pull against the file's own origin,
never gossiped, never passed through `handle_events`. `remote_files` is a
new table, not a row in the real `files` table — that table's own invariant
("bytes always exist before the row does") stays true unconditionally, and
a catalogued-but-not-fetched file is a state `files` was never designed to
represent; `netbbs.files.storage`'s existing content-addressed layout is
reused once a transfer completes and verifies, so a fetched file dedups by
hash automatically, same as a local upload. `transfer_id` is deterministic
(`file_id` + this node's own fingerprint), and `chunk_id` (the chunk's own
sha256) is the exact-dedup key for a resent/duplicate chunk request —
found and fixed a real bug while writing tests: `materialize_carried_file_
descriptor` initially keyed `remote_files.file_id` off the signed *event's*
own `content_id` rather than `payload["file_id"]` (the file's actual local
identity) — two different hashes for the same object, the same distinction
`FileDescriptor`'s own docstring already calls out as the reason `file_id`
is carried explicitly in the payload rather than reusing `BoardPost`'s
"the event's content_id already is the object's identity" precedent.

Bounded per §13.5: `max_carried_file_areas`/`max_remote_files_per_area`
(carry-limit errors, tolerated the same way `BoardCarryLimitError` already
is), and `max_concurrent_file_transfers_per_peer` on the serving side,
tracked in memory only (never persisted — serving one chunk is otherwise
fully stateless). Chunk transfer is deliberately not folded into issue #60's
work-item/DLQ abstraction, the same "not every retry-shaped mechanism fits"
reasoning §13.7 already documents — it already has a natural resumable-by-
construction terminal state.

**Explicitly out of scope at the time, tracked separately and since
closed:** file-area origin succession remains untracked (no issue yet);
inventory/pull catch-up (§8.8) extended to file areas was deferred here and
closed by issue #93 (§11.4); a live SysOp/user TUI action to browse a
remote area's catalogue or trigger a fetch was deferred here and closed by
issue #92.

### Issue #91 — wire linked-channel messages into the live chat send path — closed

Closes the gap issue #87's own §16 entry named: `netbbs.net.chat_flow._chat_
loop`'s message-send path now threads an optional `link_context` (mirroring
`netbbs.net.board_flow._show_board`'s own parameter for board posts exactly)
down from `browse_channels`, and calls `netbbs.link.channels.queue_channel_
message_if_linked` right after a self-authored message is locally recorded,
whenever `channel` is Linked. Fire-and-forget, no separate success/failure
message shown to the sender — the same shape `queue_board_post_if_linked`'s
own call site already established; the actual outbound push, and its own
failure handling, lives entirely in `netbbs.link.sync`'s existing background
loop, unchanged by this issue. An unlinked channel, or a session with no
`link_context` at all (Link disabled, or a caller bypassing `netbbs.net.
login_flow.handle_session`'s real wiring), behaves exactly as before —
local chat stays fully usable and Link-unaware.

Minimal threading, no broader `chat_flow` refactor: only `browse_channels`/
`_chat_loop` gained the new parameter, and only the three existing `netbbs.
net.login_flow` call sites (`[N]ew scan`, `[/] Find`, the main channel-browse
menu) needed updating to pass their own already-in-scope `link_context`
through. A real two-node end-to-end test (`tests/test_link_end_to_end.py`)
drives `_chat_loop` itself with a scripted `FakeSession`, not a direct
`queue_channel_message_if_linked` call, proving the interactive send path
specifically, per the issue's own acceptance criterion.

### Issue #92 — interactive browse/fetch UI for remote file catalogues — closed

Closes the UI gap issue #89 left open: `netbbs.net.file_flow._show_area`
gains a remote-catalogue screen (reachable from both the ordinary paginated
file listing and the "has no files yet" action bar, since a Linked area can
have remote catalogue entries with zero *local* uploads of its own),
offered whenever an optional `link_context` is given — threaded down from
`enter_file_area`/`browse_file_areas` the same way `netbbs.net.login_flow`'s
board/channel paths already thread it. It lists every catalogued
`RemoteFile` for the area via `netbbs.link.files.list_remote_files`
(fetched and not-yet-fetched alike, clearly labeled, so a user can tell
"catalogued and I already have it" from "catalogued and I don't" at a
glance — no separate hidden state); picking an already-fetched entry
reports that and stops, never re-offering a redundant fetch. Picking a
not-yet-fetched entry, after a yes/no confirmation, drives `netbbs.link.
transport.fetch_next_file_chunk` in a loop until the transfer completes,
fails, or the origin turns out unreachable (`dialable_base_urls_for_peer`,
new — chunk transfer is never relayed, so an origin with no advertised
direct address is reported clearly rather than attempted). Success is
reported once the transfer's own existing verification/promotion path
(unchanged by this issue) has already placed the content in the ordinary
local `files` table; the file is then reachable through the listing's
pre-existing download keys like any other, no new download path introduced.

No per-file access check inside the catalogue screen beyond what already gated
entering `_show_area` in the first place (design doc's own "merely knowing
a descriptor exists must not bypass local policy" acceptance criterion) —
a `RemoteFile` carries no independent moderation state of its own the way
a pending local upload does, so the area-level read/age/name-requirement
gate already enforced by whichever picker offered the area is sufficient.

`aiohttp`/`netbbs.link.transport` are imported lazily, inside the one
function that actually dials out (`_fetch_remote_file`) — `netbbs.net.
file_flow` is loaded unconditionally by every node, including one with
`aiohttp` not installed, matching the same lazy-import convention
`netbbs.__main__`'s own Link-server startup already established.

A full interactive-flow regression test (`tests/test_link_end_to_end.py`)
drives `_show_area` itself against a real second node — the
`[L]ink catalogue` key, `pick_item` selection, the fetch confirmation prompt
— proving browse -> fetch -> verify/promote -> ordinary download visibility
end to end, per
the issue's own acceptance criterion; `tests/test_file_flow_remote.py`
covers the UI-level edge cases that don't need a real second node (no
catalogue entries, an already-fetched entry, a declined fetch, an
unreachable origin).

### Issue #93 — inventory/pull catch-up for linked file-area catalogues — closed

§11.4 states the complete design and is now fully implemented: `Inventory
Request` gains a third `file_areas` key alongside `boards`/`channels`, the
identical shape issue #87 already added for channels; `netbbs.link.store.
file_area_event_diff`/`_all_file_area_events` mirror `board_event_diff`/
`channel_event_diff` exactly, unioning the same three sources (self-
originated genesis, self-authored descriptors, peer-received `link_events`
rows) those functions already established. `_handle_inventory` now shares
one overall `_MAX_EVENTS_PER_REQUEST` budget across all three diffs in
sequence (board, then channel, then file area) rather than three
independent caps.

`link_events` gains a nullable `file_area_id` column, populated at two
call sites rather than one: `netbbs.link.store.save_event`'s generic
dispatch (for `file_area_genesis`, mirroring how `channel_id` is populated
for `channel_genesis`) and `netbbs.link.files.materialize_carried_file_
descriptor`'s own direct insert (for `file_descriptor`, which — like
`board_post`/`channel_message` before it — skips `save_event` entirely).
`sync.py`'s inventory-response step also gained `max_carried_file_areas`/
`max_remote_files_per_area` parameters, threaded through from `__main__.py`
the same way `max_carried_boards`/`max_carried_channels` already are — a
real pre-existing gap, not a cosmetic one: before this issue, an inventory
response could carry an unbounded number of new file areas/descriptors
even though `LinkServer`'s direct-push path already enforced these same
two quotas (§13.9).

No restart-reconstruction changes were needed: issue #89's own `load_link_
node` work already rebuilt `node.file_areas` from both sources this
issue's diff query also reads, and `file_descriptor` has no chain state
beyond `known_event_ids`/`events` to begin with. Proven with the same two-
layer test shape issues #85/#87 established: a deterministic three-node
`ScriptedTransport` test (`tests/test_link_convergence.py`) proving the
protocol-level multi-hop mechanism in isolation, and a real-transport test
(`tests/test_link_end_to_end.py`) proving a node recovers a missed `file_
descriptor` through an intermediary while the original origin's server is
never started again for that stage — genuinely unavailable, not merely
unqueried. Chunk bytes remain outside inventory entirely and unchanged by
this issue, confirmed by both tests: a recovered catalogue entry lands
with `fetched_file_id` still `NULL`.

### Issue #55 — trust and quarantine — implemented; operational validation open

§12 specifies the Phase-4 attacker model, evidence classes, explicit reporter
trust domains, Sybil/weight rules, signal bounds and lifetimes, probation,
quarantine, reversibility, explainability, and required validation. The Phase-4
implementation is shipped; #131 retains the human/operational readiness gate.
Completing the implementation does not itself establish public federation safety.

### Issue #63 — door isolation — superseded by the shipped door model

Issue #172 established the native door model; #296/#297 added legacy and browser
adapters. Phase 7 above records the current contract. Native doors are trusted,
same-user programs with resource limits and supervised cleanup, not filesystem
or network containment. Door metadata is API version 3; companion services and
the SysOp-enabled outbound board-posting hook are implemented.

The [developer handbook](NetBBS-Developer-Handbook.md#developing-a-native-door)
is the integration entry point. The outbound hook posts as a distinct door label,
with an explicit per-door board allowlist and rate ceiling. A linked destination
can federate the post through the live node's identity. There is no generic
session-capability API for chat, mail, real-time game moves, or federated scores;
those extensions need their own protocol and authority decisions.

### Issue #474 — foreign-platform native doors in a VM — closed

A door that exists only for another platform -- the motivating case was
`aempire`, a statically linked Linux x86_64 Free Pascal door with no NetBSD
build and no source -- runs under `adapter: "vm"`: one qemu guest per caller,
booted from a kernel and initramfs the SysOp builds. The Phase 7 bullet above
states the runtime contract; the door guide's "Foreign-platform doors in a VM"
states the guest image contract and the manual setup.

**Measured, not assumed.** The scoping questions were answered on the
production host that motivated it (NetBSD 11.0 amd64, itself a VMware guest,
qemu 11.1.1 from pkgsrc):

| Question | Answer |
| --- | --- |
| Q1: does 9p `local` work on a NetBSD host? | Yes. Both exports mount (`9p2000.L`, `cache=none`); guest and host each see the other's writes. The persistent installation stays a directory, so backup and "install with ordinary file tools" are unchanged. |
| Q2: does `-chardev socket,fd=N` accept an inherited descriptor? | Yes, for the door's terminal and for a second socketpair carrying QMP. |
| Q3: qemu's address space? | About twice guest RAM plus 500 MiB: a 256 MiB guest fails at 768 MiB and starts at 1000; with `tb-size=64`, steady VSZ ~500 MiB. Validation requires `memory_mb >= 2 * guest_memory_mb + 512`. |
| Q4: cold boot? | 3.65 s ±0.05 to the door's first byte (microvm, PVH direct kernel boot, TCG), 3.7-4.0 s through the full runtime. `pc` with PVH: 6.4 s; `pc` with a bzImage: 7.7 s. |
| Q5: grant `/dev/nvmm`? | The operator accepted it (2026-09-12). Moot on this host: `nvmm` refuses to load there (`cpu not supported`), so everything above is software emulation (TCG). |

**Adapter, not runner.** A `runner` script could spawn qemu, but host
placeholders mean nothing inside a guest, and only a command line NetBBS
builds can *guarantee* `-nic none`, `-nodefaults`, `-no-user-config`, no
display and exactly two exports. `vm.py` owns the argv the way `dosbox.py`
owns `dosbox.conf`; the profile supplies paths, the guest command, the
accelerator and sizes, never qemu flags.

**Per-session, at 3.7 s.** The rescoping comment on the issue put the
threshold at ~3 s for per-session and ~5 s for a long-lived guest serving
callers as a door service (#466). The measurement fell in between, and
per-session was chosen: it keeps isolation per caller rather than per door,
needs no multiplexing inside the guest, confines a guest crash to one
caller, and reuses the existing reap-the-group lifecycle unchanged. A
caller waits about as long as a DOSBox-X start. The measurement is on the
slowest configuration there is -- nested virtualization with no hardware
acceleration -- so the choice only gets better where `nvmm` or `kvm` works.
A VM-backed door service remains possible later if a busy node needs it.

**Channel.** The door's existing socketpair becomes a virtio console
(`virtconsole`, `/dev/hvc0` in the guest): the tty lives in the guest, so
the host PTY's edge cases do not apply, and the door gets a real terminal.
vsock (Linux-only host support), SSH into the guest (needs guest networking
and a credential) and a bespoke multiplexer were rejected, as scoped.

**Exit status and stop.** qemu exits 0 whatever the game did, so the guest
writes the door's status to `exit.status` in the node export; missing or
unreadable is a failure, and `success_exit_codes` maps normal returns, as
`EXIT.ERR`/`RETURN.OK` do for DOS. The guest touches `booted` once its door
is about to start, which bounds a guest that cannot boot by
`boot_timeout_seconds` instead of the caller's time limit. qemu exits on
SIGTERM without informing its guest, so every stop path first presses the
guest's ACPI power button over a private QMP socketpair (the guest hangs
the door up and powers off), then signals the process group as for any door.
qemu stays in that group, so the kill still guarantees no guest outlives
its session.

**What the boundary buys.** A guest cannot read the node's database, keys,
configuration, other doors or anything outside its two exports, and has no
network -- strictly more than a native door. It is still the SysOp's
boundary: the installation export is writable, qemu runs as the service
account, the guest image and its patching are the SysOp's, and an
accelerator is a host-wide privilege grant. Documentation says "an isolation
boundary you provision and own", never "sandboxed doors".

**No shipped image.** NetBBS ships a build recipe
(`examples/doors/vm/build-alpine-guest.sh`, from Alpine's own packages) and
the reference init, not an image whose security updates it would own
forever. The capability probe boots the SysOp's own image with a NetBBS
fixture script and checks the console in both directions with CP437, a
write through the installation export and the exit-status handshake. It can
only be operator-run: unlike the DOS probe, it needs an image that no CI
carries. The runtime contract is instead tested against a fake qemu that
plays the guest's half over the descriptors it is given.

**Deliberately not built.** Resize forwarding into the guest (the console's
geometry is fixed at launch), guests for other architectures (the adapter
is `qemu-system-x86_64` and `microvm`), and the sibling `ssh-exec` adapter
for running a door on a second machine where it is native.

### Issue #165 — MRC gateway scoping — closed

**Goal:** let NetBBS callers reach the existing cross-BBS MRC (Multi Relay
Chat) network without coupling NetBBS's own premium chat model — Noise
XX transport, Link-wide presence, per-message trust-filtered scrollback,
all closed by issue #164 — to a third-party network's pace or ceiling.
Sequenced after #164, which is done (shipped in v5.3.0): this scoping
pass was unblocked the moment that landed.

**Reference protocol, since no formal MRC spec exists:** verified
directly against ENiGMA½ BBS's own reference client/multiplexer
(`core/mrc.js`, `core/servers/chat/mrc_multiplexer.js`), the most
actively maintained modern implementation. MRC is a single central hub
(historically `mrc.bottomlessabyss.net`), not a federated or
peer-to-peer network — every participating BBS is one more client of the
same hub. The wire protocol is a single persistent TCP socket (TLS
optional, separate port), newline-delimited, tilde-separated 7-field
lines: `from_user~from_site~from_room~to_user~to_site~to_room~body~`.
`to_user`/`to_room` double as addressing and as an ad hoc control
channel (`to_user="SERVER"`/`"CLIENT"` carries heartbeat/roster/registration
commands like `IAMHERE`, `USERLIST`, `STATS`, `LOGOFF`, `INFOSYS`/`INFOWEB`/
etc.). The only "handshake" is one unauthenticated line the client sends
on connect — `{boardName}~{clientSoftware}/{os}/{version}` — no password,
token, or signature ties a connection, a user name, or a claimed board
name to anything real; any client can claim to be any board. User and
site name fields are constrained to ASCII 33–125, 30 chars, room names
to 20 (the protocol page's `string[20]`, issue #376), with Mystic `|NN`
pipe-color codes stripped; message bodies to ASCII 32–125. There is no
history or
backfill concept at all — a message reaches only whatever clients happen
to be connected at the instant it's sent.

This resolves several of the issue's own open questions directly, rather
than leaving them for a later pass: MRC has no identity a gateway could
verify, so nothing about it can ever feed Phase 4's trust/reputation
model, and nothing resembling NetBBS's own trusted-scrollback concept
has an MRC-side equivalent to bridge to.

**Existing precedent this reuses, not reinvents:** `netbbs.link.
realtime_channels.LiveChannelBridge` is the architectural template — one
instance per running node, holding no storage of its own, forwarding
everything through the same `netbbs.chat.hub.ChatHub.broadcast()` a
purely local participant's own message already goes through. Its own
`_handle_channel_message` already renders a remote-authored message with
`author_fingerprint=None` and a descriptive `author_label` (`f"{user_id}
@{fingerprint}"`) — the shape an MRC-authored message reuses, with one
storage rule added on review: `channel_messages.external_source` marks
such a row so it is never attested as this node's own content (§6.3;
trusted-scrollback snapshots skip it and Link queueing refuses to sign
it). The outbound hook
point already exists too: `netbbs.net.chat_flow`'s per-message send path
calls `link_context.realtime_bridge.broadcast_local_message_live(channel,
recorded_message)` immediately after `record_message`/`hub.broadcast` —
a sibling MRC bridge attaches at that identical call site, independent
of `link_context` and of whether Link itself is even configured.

**Decision 1 (locked in) — one in-process bridge per running node, no
separate daemon.** Unlike ENiGMA½'s own two-tier design (a per-connection
client process plus a separate local "multiplexer" process fanning
multiple local sessions into one hub connection), NetBBS is already a
single asyncio process per node — there is no per-connection-process
architecture here to multiplex in the first place. One `MrcBridge`
instance, analogous to `LiveChannelBridge`, owns the one outbound hub
socket for the whole node.

**Decision 2 (locked in, amended by issue #300) — a local channel is
bridged only per channel, explicitly, and off by default; an MRC room a
caller opens is a channel of its own, behind a second node-wide switch
that is also off by default.** MRC rooms are flat, global, and
unauthenticated with no ACL concept at all — "every local channel
bridges by default" would silently leak channel contents onto a public,
unauthenticated network the moment a SysOp enables MRC at all. A SysOp
must name which local channel maps to which MRC room, the same opt-in
shape every other Link-adjacent per-channel setting already uses. The
amendment (issue #300) keeps that rationale intact: an *open room* has
no local content to leak because it is born on the network — a caller
asks for a room by name and it materializes as a real channel named
`mrc:<room>` (design doc §6.3), so enabling MRC alone still bridges
nothing, and the SysOp decides separately whether callers may open
rooms at all.

**Decision 3 (locked in, amended by issue #305) — channels by default;
private messages only for a caller who asked for them.** The protocol
argument stands: MRC's `to_user` targeting carries no real
confidentiality (the hub, or any client willing to lie about its own
identity, can see or spoof it), so NetBBS must never present an MRC
private line as private. What the original decision got wrong is the
remedy: refusing them outright leaves a caller who reads "bob@Other
tried to message you privately" with no way to answer, while every
other client on the network delivers such lines. So the bridge delivers
and sends private lines for a caller who switched them on in their
Profile (off by default, one switch for both directions), tells them
once per session what "private" means on that network, and never
records a private line anywhere. A caller who left the switch off gets
exactly what they got before: one notice per sender, nothing shown.

**Decision 4 (locked in) — inbound content is always rendered as
external/untrusted, and never enters Phase 4 at all.** An inbound MRC
message becomes a `ChannelMessage` with `author_fingerprint=None` and an
`author_label` such as `f"{mrc_user}@{mrc_site} (MRC)"` — visually
distinct from both a local and a genuine Link-originated author label.
It is never passed to `decide_node_action`/quarantine/trust scoring:
there is no cryptographic identity on the MRC side to hang a trust
decision on, so inventing one would be theater, not protection.

**Decision 5 (locked in) — bounded failure containment, matching this
project's existing "bound remotely influenced resources" invariant.**
Reconnect-with-backoff on the one outbound socket (mirroring the retry
behavior every MRC implementation already converges on); a bounded
outbound send queue so a stalled or unreachable hub degrades to that one
bridge going quiet, never blocking local chat delivery or the caller who
just sent a message; malformed or oversized inbound lines are dropped
and logged, never allowed to crash the local channel. Every outbound
field is sanitized to MRC's own documented charset/length limits before
it's sent. Inbound Mystic `|NN` pipe codes are untrusted text, never
raw ANSI: identity fields lose all of them, a body keeps only the
color subset (`|00`-`|23`), and those become NetBBS's own SGR solely
through `netbbs.rendering.pipe_codes`, after sanitization and per
viewer (issue #298, below) — "sanitize before styling" holds exactly.

**Implemented as issue #275** (`netbbs.mrc`), with every decision above
as specified and the three questions this scoping pass left open
answered as follows. Two further choices were made at implementation
time, both verified against four independent MRC implementations
(ENiGMA½, Synchronet, uMRC, ANetBBS) since the hub operator's own spec
page is not publicly reachable:

- **Identity on the MRC side is one MRC user per caller** — the
  caller's canonical username sanitized to MRC's rules (spaces to
  underscores, printable ASCII, 30 characters, reserved routing names
  suffixed), presented at this node's site name, with `NEWROOM` on
  entering a bridged channel, `IAMHERE` every minute and `LOGOFF` on
  leaving. Every reference implementation multiplexes this way; a
  single relay identity prefixing each line with the handle was
  rejected because MRC users could not see who is present or address
  anyone. The announced set is derived from the `ChatHub` roster, so a
  channel mapped while callers are inside it, or a reconnect after a
  hub outage, re-announces exactly who is there. The join banner tells a
  caller their handle is about to appear on a public network before
  the announcement is sent.
- **Inbound room messages are recorded into the channel's scrollback**
  (`record_message`, `author_fingerprint=NULL`, label `user@site (MRC)`),
  bounded by the existing scrollback limit and search-indexed like a
  carried Link message, so a caller joining later sees a coherent
  conversation rather than only the local half; the hub's own echo of
  this node's traffic is dropped by site name. Presence chatter, room
  topics and server notices stay ephemeral. Private (`to_user`) lines
  are never delivered; the addressed caller gets one muted notice per
  sender.
- **Configuration is DB-backed and edited live**, not a TOML section:
  Settings › Inter-BBS chat (MRC) holds the node-wide hub settings
  (enabled, host, port, TLS on by default with certificate
  verification, site name, INFO fields) and saving asks the running
  bridge to reload and reconnect; a channel's own detail screen holds
  its `[M]RC room` mapping and a `[P]ause` that keeps the mapping while
  relaying nothing — the per-bridge disable asked for above; Node ›
  Chat bridge (MRC) shows link state, hub, last error, attempt and drop
  counters and every bridged channel with the hub's roster, with
  `[R]econnect now`. Inside a bridged channel, the status line carries
  an `[MRC]` badge, `/who` and `/names` list the room's MRC users, and
  `/mrc` shows room, hub, link state and roster.
- **Bounds as shipped:** reconnect backoff 1–60 s with jitter, reset
  after 30 s stable; outbound queue of 200 lines (oldest dropped) behind
  a node-wide 5 lines/s bucket and a 3-line burst / 1 line/s bucket per
  caller (the sender is told quietly when a line was not relayed), and
  on the way out packets from one nick are spaced at least 0.5 s apart
  -- the hub's own per-user limit (MRCDoc rev 1.26; issue #375) -- with
  held-back lines counted under the same 200-line cap; the spec's field
  limits are enforced where a caller or SysOp types (room names 20,
  refused rather than cut; topics 55; passwords 20, room passwords 32;
  LASTSEEN and HELP arguments 20), and the handshake names the client
  as `NETBBS/<Os.arch>/<NetBBS version>` (issue #376);
  inbound 40-line burst / 20 lines/s ahead of any database write, 4 KiB
  line cap; a local line is split into at most three 140-character
  wire chunks. An `OLDVERSION` rejection from the hub is fatal until a
  SysOp changes settings, since every retry would start a fresh
  rejected session. Bridge warnings land in the same bounded
  diagnostic log Link already uses.

**Issue #298 (body convention, colors, CTCP, per-caller hub replies)**
corrected one shipped assumption and added what falls out of it. Every
reference client embeds the sender's own colored handle *inside* the
message body (Mystic `|03<|11Alice|03>|16|07 text`, ENiGMA½
`|00|10<|02Alice|10>|00 |03text`, Synchronet `Alice |07text`, ANetBBS
`|07Alice|07 text`; actions `|15* |13Alice text`) and displays an
inbound body verbatim; the hub adds nothing. The bridge therefore
sends every chunk in a house style (`|08<|14nick|08>|16|07 text`, the
prefix paid for out of the same 140-character budget) and peels an
inbound prefix only when the embedded name equals `from_user` -- an
unmatched body is recorded whole, nothing is guessed. Decisions taken
with it:

- **Color codes are content, not markup.** `|00`-`|23` survive in a
  stored MRC body (printable ASCII, safe to store) and are turned into
  SGR by `netbbs.rendering.pipe_codes` only after sanitization, per
  viewer, under a Profile toggle that defaults to on; every other pipe
  token is stripped at the parse boundary, identity fields lose all of
  them, and the search index receives the plain words. The recognized sender
  prefix's foreground is stored separately as `mrc_nick_color` and applied
  to the author on live display and replay under the same color toggle.
  Old rows keep their text and default author styling. A caller's own
  typed codes are still stripped: letting them through would put codes
  into local scrollback and Link-signed exports of mapped channels.
- **The hub's reply to one caller goes to that caller.** A `SERVER`
  packet addressed to an announced nick is that caller's reply
  (`LIST`, `CHATTERS`, `INFO`, `MOTD`, `HELP`, ...), delivered to their
  sessions alone under a per-caller line allowance, never recorded;
  `/mrc <subcommand>` sends the asks. `USERROOM` re-announces the
  caller in the mapped room at most once per keepalive tick,
  `USERNICK` retargets the announced nick. Generic-client corrections apply
  only when exactly one caller is announced; with multiple callers they
  produce a bounded diagnostic and a caller notice without guessing an
  identity. Generic-client `STATS` replies update the network summary and
  are shown only to callers with outstanding explicit requests.
  `TERMINATE` is fatal like
  `OLDVERSION`. An empty `to_room` is a network broadcast, shown in
  every active bridged channel (ENiGMA½'s reading).
- **CTCP lives in the bridge.** `VERSION`, `TIME` (UTC only), `PING`
  and `CLIENTINFO` are answered for any announced nick, bounded per
  remote sender because every request costs a reply; no SysOp or caller
  wiring exists for it.

Decision 3 (channels only) is unchanged by this issue; its amendment
(opt-in private messages) shipped as issue #305, below. Decision 2's
amendment shipped as issue #300, next.

**Issue #300 (open rooms)** lets a caller reach any room on the network
without a SysOp mapping it first, without a door-style client, and
without teaching native chat anything about MRC. Decisions:

- **An open room is a real channel row, materialized on demand** — the
  same precedent as a carried Link channel (§9.6): the first caller to
  open `lobby` gets a `channels` row named `mrc:lobby` with
  `mrc_room='lobby'` and `mrc_origin='caller'`, gates copied from the
  node-wide open-room defaults (level, age, name requirement), content-
  addressed on the room name so a room reopened after retirement keeps
  its id. A room the SysOp already mapped *is* that channel (one room,
  one channel), so opening it lands the caller in the SysOp's channel
  under its own gates. Every existing chat feature works unchanged
  because nothing but the origin marker differs.
- **The `mrc:` prefix is reserved.** No local user or SysOp screen may
  create or rename a channel into it (`netbbs.chat.channels` refuses),
  so a hub `lobby` never collides with a local `lobby` and no local
  channel can impersonate a room.
- **Lifecycle is bounded and SysOp-tuned:** a cap on open rooms (default
  32; opening refuses past it, nothing is evicted), a retention period
  (default 7 days) after which a room with no local participant, no
  activity and no follower is retired by the bridge's sweeper together
  with its scrollback, a blocklist of rooms callers may not open — or
  enter, once blocked — and `[A]dopt`/`Re[t]ire` on the channel
  screen. The sweeper runs on its own task, independent of the hub
  connection and of the switch, so a long outage or switching MRC off
  never strands the cap. It is a node action with no `User` behind it
  and the moderation log requires an actor, so a retirement is reported
  to the MRC diagnostic log and counted on the status screen instead of
  audited; a SysOp's own retire is audited as usual. The gates are
  checked before a room is materialized, so an account they turn away
  cannot spend the cap on rooms it can never enter.
- **Never Link, in either direction.** `link_channel` refuses a
  caller-origin row and the channel screen offers no `[L]ink` for it:
  MRC content is that network's, not this node's, and must never be
  re-broadcast under this node's signature. Inbound, a peer's genesis
  named into the `mrc:` prefix is carried as `local-mrc:...` (a channel
  Linked before the prefix was reserved keeps propagating; a squat
  lands nowhere), one claiming an open room's id is refused outright,
  and a carried message is projected only into a channel with a genesis
  on file. An adopted room keeps its `mrc:` name until the SysOp
  renames it, and Link asks for that rename before signing. An open
  room's id is content-addressed on the
  room *and* a per-node secret, so two nodes opening the same room
  never share an id and no peer can compute one. Adopting a room
  clears the origin and makes it an ordinary mapped channel again,
  which may be Linked like any other. The migration renames any local
  channel that already wore the prefix (it was typeable before this
  release) to `local-mrc:...`.
- **One MRC identity per account, in one room.** The hub knows one
  `nick@site` in one room; a second session of the same account
  entering a different bridged channel is refused naming the room the
  identity already holds. The rule is decided from local occupancy of
  the bridged channels, not from what has been announced to the hub
  (announcements lapse during backoff), and enforced once, atomically,
  at the chat loop's own hub join. The session that is leaving a room
  on `/join` does not count against itself unless another session of
  the same account stays behind. A nick suffix was rejected: it would
  present a second person to the network.
- **Finding rooms.** With MRC and open rooms enabled, Chat's Multi Relay
  Chat section lists retained rooms and discovered network rooms with hub
  user counts and topics. A fresh directory offers `lobby` as a starting
  point, plus opening a room by name. Entering a room requests `LIST` in
  the background, also on reconnect and roster refresh, at most once per
  five minutes per connection without spending callers' message allowance;
  `/rooms` refreshes it
  explicitly and shows a readable listing. The first MRC room of a
  session answers the background listing with one line -- how many more
  rooms there are, and that `/rooms` lists them and `/join <room>` moves
  there -- from the listing already in hand when the five-minute floor
  suppresses the request; never the table. Discovery never opens the
  listed rooms. Local access rules and the blocklist still gate entry.
  Refusals stay in the picker's header through redraws. Open rooms appear
  in this section, not the ordinary channel list. Inside MRC, `/join <name>`
  finds an open MRC room, then a local channel, then opens an MRC room;
  `/join mrc:<name>` opens or finds that MRC room from anywhere.
  `/join <room>` and `/mrc` subcommands and recipients have completion.
- **Directory evidence.** The hub's room-list layout observed during live
  use supplies the parser's anchored rows. Only replies to recent `LIST`
  requests populate the bounded cache; unfamiliar output remains text,
  never guessed room names. Counts and topics are advisory snapshots, with
  stale readings labeled with their age. Reconfiguring the hub clears its
  directory and observed room names, then reloads locally retained
  mappings. Unknown
  generic-client LIST lines stay scoped to explicit requesters and are
  suppressed for automatic requests, never broadcast to other callers.
- **The directory outlives the run (issue #636).** The hub lists rooms
  only to a user who is already in one: `LIST` is a session-context verb
  (MRCDoc protocol page, rev 1.26), and a probe of the development hub on
  2026-09-18 met both a client-context `LIST` and a `LIST` from a nick
  never announced with `NEWROOM` with silence. So the node never asks by
  itself -- no caller-less request, and no service nick parked in a room,
  which would put a fake user on the network from every NetBBS node.
  Instead the last *complete* listing (rows closed by the hub's footer)
  is kept in `node_config`, bound to the hub's host and port, bounded to
  the directory's own cap, every field validated again on load, and
  readings older than seven days dropped; after a restart the section
  shows those rooms at once, each with its real age, until a caller's
  entry refreshes them. A complete listing replaces rather than only
  adds: a room the hub no longer lists has emptied (an MRC room exists
  only while someone is in it) and leaves the directory, unless chatter
  or a caller named it after the listing was asked for. A footer with no
  row before it proves nothing and changes nothing.
- **MRC on, nothing reachable.** Decision 2 stands: switching MRC on
  bridges nothing. With open rooms off and no active mapping, saving the
  settings says that no caller can reach MRC yet and names both ways out.
- **Occupancy.** The status line separates local participants and their
  local away count from the hub's remote roster count. An absent roster
  reads `?`, not zero; stale snapshots are marked. MRC roster replies do
  not supply structured away flags, so NetBBS does not invent a remote
  away count. The status refreshes at most five seconds after roster
  changes even in an otherwise quiet room.

**Issue #304 (presence and welcome)** carries the presence a caller
already has here onto the network and lets the network's own life show
through, without teaching native chat anything about MRC:

- **Away state is mirrored, never separate.** NetBBS's `/away` is the
  one away state; the bridge sends the hub `STATUS AFK <message>` and
  `IAMHERE:AWAY` for every room the caller is announced in, repeats
  them on every announcement (reconnects included) so the hub is never
  behind, and reports `IAMHERE:ACTIVE` on return. Those are the
  documented forms (MRCDoc protocol page, rev 1.26, read 2026-09-09;
  issue #373): there is no `AFK` verb, the away message is 55
  characters, and the spec names no verb that clears AFK -- the hub's
  own activity tracking decides when a returned caller stops showing
  as away.
- **The hub's welcome, once per session.** On a caller's first MRC room
  in a session the bridge shows the hub's `BANNER:` lines as remembered
  from connect and asks for `MOTD` as that caller; the reply reaches
  them alone through the per-caller path of #298. Once per session, not
  per room.
- **The network's size where callers look for company.** The bridge asks
  `STATS` once per roster refresh, parses `bbses rooms users` (the
  Mystic layout; an unparseable reply keeps the raw line for the status
  screen and shows "unknown" elsewhere), and Who's online, the picker's
  MRC section and the bridge status screen show "N users on M boards"
  with the reading's age. The bridge's own ask is parsed, not shown; a
  caller's `/mrc stats` is shown.
- **An open room's topic is the hub's.** Inbound `ROOMTOPIC` for an
  open room is stored on the row (a direct update; the node is not a
  `User`) so the status line shows it; `/topic <text>` inside an open
  room sends `NEWTOPIC` and the hub decides (MRC Trust applies there),
  a bare `/topic` shows the topic rather than clearing it. Inside a
  mapped channel `/topic` keeps its local meaning and audit.
- **Identity commands with masked input, nothing stored.** `/mrc
  register`, `/mrc identify`, `/mrc update password` and `/mrc
  roompass` ask for the secret separately with echo off and send it
  once as the caller's nick; the secret reaches no scrollback, log or
  input history (decision 5 stands: the node stores no credentials that
  are not its own).
- **A personal nick color.** The house-style body's handle color is a
  per-caller choice among the sixteen CGA colors (Profile, default
  yellow), read when the caller is announced; brackets and text color
  stay the house's, and typed pipe codes stay stripped (#298).

**Issue #305 (private messages, opt-in)** amends Decision 3 as the
planning pass intended:

- **One switch, off by default, both directions.** `[P]rofile` →
  `[P]rivate MRC messages` (Communication section). The bridge reads it
  when it announces the caller, alongside the nick color, and forgets
  it with their last announcement; switching it on applies the next
  time they enter an MRC room. Sending requires the same switch:
  starting conversations while refusing replies is not offered.
- **Delivered as a notice, never as chat.** An inbound private line for
  a caller who opted in is an `MrcNotice` of kind `private`, shown as
  `[MRC private] bob@Other: text` with the bell, to that caller's
  sessions only, through the per-caller path of #298 and bounded per
  remote sender ahead of it (a flood from one nick must not evict
  chat). The sender's embedded handle is peeled exactly like a room
  line's and the color codes kept. It is never recorded: no scrollback,
  no search index, no log body.
- **Sent as the caller's nick, in the house style.** `/mrc msg <nick>
  <text>` and `/mrc r <text>` (to whoever last messaged them this
  connection) send a packet with `to_user` set and fields 5 and 6
  empty, the spec's private-message transaction (MRCDoc protocol page,
  rev 1.26; issue #374). Field 5 is `MsgExt`, reserved for extensions
  such as encryption, never routing -- "remove any reference to
  ToBBS" -- and the hub routes on the nick, which `USERNICK` keeps
  unique across boards. The site a nick was last seen at is still
  learned from inbound traffic, bounded, but only for the `nick@site`
  the sender's own echo shows. The body wears the house style so the
  recipient's client shows who wrote it; chunking and the caller's own
  send allowance apply as for a room line. The sender sees a local
  echo, never a channel line.
- **What "private" means, said once.** The first private line a
  session sends or receives comes with one note: private MRC messages
  are not private on that network; the hub and any client can read or
  spoof them. Once per session, whichever direction comes first.
- **Nothing for the SysOp to configure.** The switch is the caller's;
  the node-wide MRC switch and the channel mappings already decide
  whether they are on the network at all.

**Issue #377 (what the hub is told)** follows the protocol page's
control-context verbs, read on 2026-09-09:

- **Per caller, on announcement:** `TERMSIZE` always (the hub formats
  wide replies with it; it says nothing about the person); `USERIP` and
  `BBSMETA` only behind two SysOp switches under Inter-BBS chat (MRC),
  both off. The infrastructure principle applies: the node provides
  the interface, the SysOp owns the decision to hand a third party a
  caller's address or level. The spec's warning that a caller without
  `USERIP` "may get removed from room traffic routing" is stated on the
  switch and in the Handbook rather than used to justify a default.
- **Per connection:** `CAPABILITIES` lists what the bridge really
  handles (`MCI`, `CTCP`, `USERROOM`, `GOODBYE`, `SSL` when on) and
  carries the bridge module's SHA256 in the spec's hash field;
  `IMALIVE` carries the process id and a timestamp the hub echoes in
  `PONG`, from which the status screen shows the round trip.
- The `INFO*` fields the issue asked for already existed (issue #275).

**Issue #378 (smaller spec follow-ups)**, same reading:

- A server `NOTIFY:` is a one-time notice shown in every bridged
  channel like a banner and never remembered; the `STATS` reply's
  fourth field is the hub's activity level, shown beside the network
  size; handles are displayed with underscores as spaces, as the spec
  asks, while matching and addressing keep the wire spelling.
- The hub is moving its identity verbs to `!helper` chat text. Typed
  as chat, `!identify secret` would be recorded in scrollback and,
  the moment the channel is bridged, relayed to the room, so those
  four helpers are refused as chat in every channel while the node
  has an MRC bridge -- a paused mapping or a local channel is no
  safer a place for a password -- with a pointer to the masked `/mrc`
  forms, and the line is dropped from the input history. Nothing else
  typed with a `!` is touched: the hub's own helpers (`!time`,
  `!weather`) are ordinary chat.
- `STATUS LASTSEEN OFF` is a caller's choice on the Profile (on by
  default, the hub's default), sent on every announcement like the
  away state.
- A CTCP request whose target is `*` (blank `to_user`) is not answered:
  every wildcard would cost one reply per announced nick.
- Hub facts worth keeping: the handshake must arrive within one second
  of connecting, the heartbeat times out at 125 s, a client may hold
  at most eight connections per address, and the operator's public
  pool is `na-multi`, `eu-multi` and `au-multi.relaychat.net` (5000
  plain, 5001 TLS) with `mrcdev.relaychat.net` for development; the
  default host stays what it was until the operator retires it.

### Issue #194 — trusted scrollback-on-join — closed

**Goal:** decide whether/how a node gets recent scrollback the instant it
live-subscribes to a linked channel. Today it gets presence plus
messages going forward only (§8.10.2's own "does not offer shared recent
scrollback"), a deliberate v1 scope cut, not an oversight — but a real
gap against this feature's own "frictionless" bar.

**Not a permanent-loss problem, and not shaped like issue #168 at all.**
Every channel message on a linked channel is already a signed, durable
`channel_message` event (`queue_channel_message_if_linked`), delivered to
every linked node eventually through the existing inventory/pull-based
catch-up path (issue #85) regardless of live-connection status.
`netbbs.link.sync`'s scheduling loop runs every `sync_interval_seconds`
(five minutes by default), so today a freshly live-subscribed channel
can sit silent for up to that long before the *existing* async path
fills in what was missed. This decision only shrinks that window at
subscribe time — it never touches durability. It also carries none of
issue #168's relay/crypto-fork complexity: `ensure_live_subscription`
(`netbbs.link.realtime_channels`) already always dials the channel's
*origin* node specifically (`channel_origin_fingerprint`), never a third
node, so there is exactly one source of truth to ask, not a multi-hop
question.

**Decision 1 (locked in) — source is the origin's own local scrollback,
sent as a new frame alongside the existing subscribe-time
`presence_snapshot`.** `_handle_subscribe` already sends a channel's live
presence roster the moment a peer subscribes (§8.10.2); a
`scrollback_snapshot` frame is a sibling addition at the identical call
site, sourced from `netbbs.chat.scrollback.get_scrollback` — the same
already-bounded, origin-policy-filtered function the local UI renders
from. A subscriber accepts the frame only from the channel's current
authenticated origin and only when its `request_id` matches a still-pending
subscribe attempt; unsolicited or late snapshots cannot fill a later join.

**Decision 2 (locked in) — bounded the same way every other snapshot
already is, and rendered once, never durably stored on the subscribing
side.** A received `scrollback_snapshot` is ephemeral and render-only —
exactly the same "Live channel messages are ephemeral node-attested
assertions" principle §8.10.2 already states for ordinary live messages.
Writing it into the subscriber's own `channel_messages` was considered
and rejected: the same content already arrives durably, independently,
through the existing async materialization path, and persisting both
would risk duplicate or conflicting rows for one message with no natural
dedup key across the two paths. Snapshot entries therefore carry the signed
event's existing content ID when one exists, letting the join flow suppress
anything already rendered from local materialized history. A caller sees
only the remaining gap once at subscribe time; the durable copy still lands
separately, on its own schedule, as it already does today.

**Decision 3 (locked in) — issue #164's author-trust rule is enforced at
both nodes.** The origin's `get_scrollback` applies its own policy first.
Each entry also carries the author identity the origin derived from the
accepted signed event (or the origin plus local user ID for origin-local
content), so the subscriber independently suppresses authors it marks
`BLOCKED` or `QUARANTINED`. Origin-local display labels are qualified with
the authenticated origin; they must never resolve as same-named local
accounts on the subscriber.

The revised snapshot attribution contract is real-time protocol v2. The Noise
identity payload declares that application version, and incompatible peers are
rejected during the authenticated handshake before either side advertises a
usable live session. The join flow reports this as an explicit upgrade
requirement rather than folding it into generic transient unavailability.
Frame versions remain an inner defensive boundary.
The subscriber reconstructs every authored display label from the attested
`user@node` identity rather than trusting the wire label. Moderation entries
retain their target label for rendering but are authorless system events, so a
target's trust state cannot suppress audit history the target did not author.

`chat/scrollback.py`'s own module docstring — "the separate, harder
question of a newly-joined Link node needing catch-up scrollback from
peers... stays explicitly deferred to whenever Phase 5 starts" — is
updated alongside this decision: Phase 5 has been active for a while now,
and this issue is that decision, not a further deferral of it.

### Issue #200 — War Dialer: async multiplayer BBS-crew door game — closed

**Goal:** decide the design for a second door-game genre alongside
Voidrunner's single-player persistent model — an asynchronous,
play-by-post multiplayer game in the LORD/TradeWars BBS-door lineage,
where actions taken by one player affect another player's persistent
state regardless of whether that player is online, and the target finds
out via a summary on their next login.

**Decision 1 (locked in) — no platform or door-API changes.** §Phase 7
already states doors run with real filesystem access and no enforced
isolation. This game owns its own SQLite database (WAL mode) keyed by
the door API's stable numeric user ID, the same pattern Voidrunner
already uses for its own save state. Cross-player shared state is
therefore the door's own responsibility, not a new NetBBS capability —
issue #63's "single-player, session-scoped only" boundary for the
platform itself is unaffected.

**Decision 2 (locked in) — resolution is synchronous, not tick-based.**
Every action resolves inside the acting player's own live door session.
No cron/daemon, matching the fact that a door process only exists while
someone is logged in. Passive accrual (territory income, Heat decay) is
computed lazily from elapsed wall-clock time whenever next read, the
standard idle-game pattern, rather than requiring a background tick.

**Decision 3 (locked in) — theme is 80s/90s BBS-scene hacker/phreaker
crews, not a generic crime-syndicate reskin.** Rival crews (Legion of
Doom/Masters of Deception energy) fighting over "exchanges" (phone
NPA-NXX prefixes); "Heat" is literal federal attention
(Sundevil-era Secret Service/FBI); this reads as the platform's own
history rather than the genre's default reskin, which every existing
Discord clone already uses.

**Decision 4 (locked in) — Rank is a single, monotonic metric doing
double duty as leaderboard score and PvP bracket gate.** Tiers Newbie/
Wannabe/Script Kiddie/Hacker/Elite/Legend. Never decreasing within a
season was the deciding constraint: a raw balance that could drop would
let a strong player deliberately sandbag into a weaker bracket to prey
on newcomers. Direct PvP (Raid) is gated to your own tier ±1; territory
contests (Root the Exchange) are intentionally *not* gated, since a
district's defense is the controller's committed garrison rather than
their full Rank, keeping territory contestable by newcomers even
against a top-bracket controller.

Rank sums season achievements, never current cash, available crew or holdings:
`crew_recruited_total*10 + exchanges_taken_total*50 + successful_raids*25 +
successful_jobs*15 + successful_operations*30 + control_rank + legacy_rank`. The capture counter records
rewarded first captures; control Rank is earned time, retained after losing a
holding. `legacy_rank` preserves previously earned capture points during upgrade
and resets with the season. Python and SQL standings scores must agree. Tiers
begin at 0, 100, 300, 700, 1,400 and 2,800 points, scaled to a four-week season
without capture farming. Rank never decreases within a season; the existing raid
bracket rule and ungated territory contests remain.

**Decision 5 (locked in) — seasons (4 weeks), separate from Rank
brackets, because they solve a different problem.** Brackets prevent
moment-to-moment exploitation (a veteran farming a specific weak
player); they do nothing about long-run stagnation, where early players
simply compound the largest crews/territory indefinitely and a
newcomer has no on-ramp. A season boundary resets
Rank/cash/crew/exchange-control, flavored in-fiction as a Fed crackdown
wiping the scene's boards.

**Decision 6 (locked in) — bounded action economy and a self-limiting
risk curve, no separate anti-snowball mechanic.** 15 turns/day on a
rolling 24h window. Base Heat gain per action (Trade Warez +2, Root Exchange
+8, Raid +10, Run a Job +5/+15/+25 by approach) decays −5/real-hour; above 80, each
heat-gaining action rolls a bust chance of `(Heat−80)×2%`, capped ~40%,
normally costing 25% cash and 20% crew and resetting Heat to 0. The adopted
crew specialty/support rules below adjust added Heat, failure recovery and bust
cash loss; zero added Heat does not remove an existing bust risk. Success chance
for both PvE and PvP actions is `attacker_crew / (attacker_crew +
defender_crew)`, clamped to [10%, 90%] so nothing is ever a guaranteed
win or loss. New accounts get 48h Raid immunity. Under the adopted slice 5
rule, every committed raid attempt grants its target 24 hours of immunity
against all attackers; login and receipt acknowledgement do not clear it.

Explicitly out of scope for v1: procedural exchange generation, a
multi-resource economy, factions/alliances, and an item/weapon shop —
plausible v2 additions once the core loop is proven, not part of this
vertical.

The original five-action game is implemented in
`src/netbbs/doors/bundled/war_dialer.py`, registered in `BUNDLED_DOORS`.
Issue #362 tracks its ordered reliability and product overhaul.

**Shared-world actions (issue #362, slice 1).** Duplicate sessions for a
player are allowed and serialize their actions against the same current
database row. There is no per-session allowance or exclusive-session lease.
Each action re-reads its actor and affected target/exchange under
`BEGIN IMMEDIATE`, revalidates allowance, cash, raid protection/bracket,
season and ownership as applicable, and commits all effects, events and its
one-turn cost together before result output. A target changed since selection
is rejected without cost. Login settlement also holds one write transaction.
Refresh, quit and disconnect never save a session snapshot; ordinary refresh
does not clear target-wide raid recovery.

World initialization checks for existing exchanges under the same write lock
as seeding. An existing nonempty world with an unexpected exchange count is
preserved and refused for explicit operator repair; automatic deletion cannot
safely reconcile conflicting ownership or earned rewards.

Every menu and pause uses one bounded input decoder. CSI/SS3 and other terminal
sequences never supply action letters. Bracketed paste and unframed bursts are
discarded; use separate single keys, since rapid queued commands are deliberately
not macros. Incomplete or excessive sequences end the session with a diagnostic,
rather than letting delayed suffixes select an action. A standalone Escape can
dismiss a pause and is ignored at menus.

**Clock settlement (issue #362, slice 2).** Screen refresh and action transactions
settle Heat and the turn allowance without clearing raid recovery.
An unused allowance has no running window; the first committed action anchors
its 24 hours. Reading, login, an unaffordable attempt and cancellation do not
start it. Existing nonzero allowances keep their stored anchor; expiry leaves
the new allowance unanchored until used.

The player's last observed timestamp never moves backwards. A clock rollback
freezes elapsed-time benefits until that time is reached again; it cannot replay
Heat decay, shorten a new turn window or backdate a capture. Heat settles before
an action adds new Heat, so a reconnect cannot decay that addition over the
earlier idle interval. A session waiting at a zero-turn menu can use an action
key after refill; commit-time state decides whether that action is available.

The exchange world's stored season also prevents season-number regression after
a clock correction, including for new and dormant callers. A capture respects
both the exchange's timestamps and the actor's clocks; the effective capture time
anchors newly incurred Heat and any newly started allowance too.


**Exchange income (issue #362, slice 2).** Login, menu refresh and committed
actions collect elapsed income. Sub-dollar earnings are retained as integer
units on the player, so frequent visits pay the same as one longer interval.
A successful capture credits the previous owner's earnings through transfer
before changing ownership, in the same transaction as the action and turn.
The previous owner keeps fractional earnings even after losing every exchange.
Neither collection nor transfer moves an exchange's income timestamp backwards.
An income-only collection does not invalidate an otherwise unchanged selection.

Existing worlds receive an additive, transactional player-column upgrade with
zero initial remainder; fractions discarded by older versions cannot be
reconstructed. Season resets clear the remainder with the other season resources.

**Atomic seasons (issue #362, slice 2).** Login, resource refresh, world/rival
views and actions settle the shared season under the same write lock as their
state access. All prior-season players, including dormant crews, reset together
with exchange ownership and garrisons. Account IDs, handles and original creation
times remain; the 48-hour newcomer grace is never renewed for a veteran.

A persisted active-season marker commits with the resets and never regresses.
Existing mixed-season worlds are adopted without erasing already-current-season
progress. Skipped seasons advance directly to the current season; there is no
old ownership income or competitive power carried through the reset. Failed
rollover writes preserve the entire previous world. Schema 10 archives the
outgoing season and final territory Rank before resetting, in this same transaction
(see season results below).

A selection made with an old-season snapshot is rejected without spending
resources. The next menu refresh shows the crackdown notice and fresh resources,
and the caller can continue in the same session. A rejected action transaction
can roll back its attempted settlement too; the following read commits rollover.
Later gameplay decisions remain governed by the locked rules above until an
explicit design pass adopts their replacements.

**Event history (issue #362, slice 2).** Keep the latest 500 events per player,
ordered by their committed IDs, with no age expiry. New receipts remove the
oldest, including unread receipts beyond that limit. Existing unbounded history
is trimmed transactionally on upgrade. Reads use target/history and target/unseen
indexes and fetch at most 500 records.

The login summary shows unread events oldest first; free `[H]istory` replays
retained events newest first with UTC timestamps and NEW/READ labels. Both views
paginate for terminal width and height down to the 40x12 floor and show the
retention limit.
Smaller terminals receive a size diagnostic without acknowledging anything.
Continuing a summary page accepts only records whose final line was displayed;
Back and disconnect leave that page unread. History navigation does not acknowledge
records; `[A]ck page` does so explicitly. Acknowledgement updates only the displayed
player/record IDs atomically, so events arriving while a page is open remain unread.
Onboarding and summary disconnects exit cleanly.

**Switchboard dashboard (issue #362, slice 3).** The main screen shows settled
cash, crew, Heat and turns; current holdings and hourly income; monotonic Rank
and the next tier's threshold; unread event count; newcomer and target-wide raid-recovery
protection; turn refill and season end time. The read obtains these from one
world transaction and never clears raid protection or acknowledges events.
The countdowns are snapshots refreshed when navigating or returning from an action.

Next/Prev pages keep action and free-browsing keys visible at 80x24, 40x12 and
40x12. Smaller terminals receive an explicit minimum-size response before opening
the world. Action outcomes, rejected actions and season-change notices wait for
acknowledgement before the dashboard clears them. Cancelling a target selection
returns directly. Detailed action previews remain a subsequent slice 3 bullet.

**Free browsing (issue #362, slice 3).** `[B]Rank` opens current-season standings,
ordered by descending Rank, then ascending stable account ID for ties. Your exact
position is shown even outside the current batch. `[E]Map` shows exchange ownership,
defenses and hourly income; `[V]Rivals` lists other crews in account-ID order with
their Rank/tier and current raid eligibility reason. This directory exposes no
additional crew-strength or cash intelligence; slice 5 deliberately keeps them private.
`[H]Log` replays events and `[?]Help` opens the rules. Every view is content-first
with Back, and costs no turns or cash. World reads still settle elapsed resources.

Standings and rivals fetch ten players per batch; Next/Prev traverses both wrapped
terminal pages and batches, reaching crews beyond the old random fifty-row sample.
Each batch is a fresh current-season snapshot, so standings can move during a visit.
Help and territory use the same width/height-aware pagination, including onboarding
help. Viewing directories does not reset raid protection or acknowledge receipts.

**Action previews (issue #362, slice 3).** Every action opens a free preview before
spending a turn. The final page offers `[A]Act`; Back and disconnect commit no action.
Previews show turn/cash cost, success stakes, Heat and bust probability. Exchange
odds use public garrisons; raids explicitly retain uncertainty about private crew
strength and cash. Jobs use the fixed contract and approach selected by the caller;
only their outcome is rolled on commitment.
Recruitment has no Heat/bust roll. Failed actions show the actual crew-loss floor.

Commit revalidates the actor's previewed cash, crew, turns, season and Rank as well
as the target selection. A conflicting action or incoming loss requires a fresh
preview. Ordinary elapsed Heat decay can lower the advertised risk. Action deltas
are calculated inside the committed transaction after income collection and include
bust losses; narration distinguishes gross payout from net cash/crew/Heat/Rank/turn
changes. Receiving an outcome never depends on another session-snapshot save.

**Contract board (issue #362, slice 6; maintainer approved).** Run a Job opens
five fixed, repeatable contracts in ascending difficulty. The ladder is dial-up
access (difficulty 2, $60-$100 Standard payout), software warehouse (6, $100-$170),
billing database (12, $170-$280), bank transfer (20, $260-$400), and payroll
(30, $380-$560). Success probability is available crew divided by available crew
plus difficulty, bounded to 10%-90%; every success earns 15 season Rank.

After selecting a contract, choose Cautious (70% payout, +5 Heat, no ordinary
failure crew loss), Standard (100%, +15 Heat, one available member lost on failure),
or Bold (140%, +25 Heat, one available member lost on failure). Payout range ends
are rounded down to whole dollars. Approaches change stakes, not success odds.
Every attempt costs one turn and no upfront cash; the one-member floor applies.
A subsequent Heat bust can still take cash and crew, including after Cautious
failure, and its risk and losses are shown separately before Act.

The contract and approach pickers are free, content-first screens with Back.
The final preview shows selected terms, current odds, payout, failure losses and
Heat/bust stakes. Only Act spends or draws randomness, under the existing fresh
actor validation and atomic action transaction. A caller with no turns can still
inspect every offer and preview. Browsing, cancellation and reconnect never
reroll offers; fixed repeatability needs no persistent offer cache or migration.

**Crew development (issue #362, slice 6; maintainer approved).** `[S]Kit` opens
the crew's specialty and support choices; `[C]rew` retains direct recruitment.
One crew-wide specialty is active at a time. Training or switching costs $150
and one turn: Phreakers reduce contract Heat by 3; Fixers recover $20 on a failed
contract, before any bust and without Rank; Lookouts reduce raid/root Heat by 3.
Training persists through ordinary crew losses and assignments until switched
or the competitive season resets. Buying the already-active specialty is rejected.

One consumable support slot holds either a $40 Burner Kit or a $75 Cash Stash;
each purchase also costs one turn. Occupied slots cannot be replaced or stacked.
A Burner Kit removes up to 10 added Heat after specialty reductions, floored at
zero, on the next committed job/raid/root attempt, win or lose. That attempt
consumes it even if fewer than 10 Heat were removed. Trade, recruitment, garrison
transfers and purchases do not consume it. Existing Heat still causes an ordinary
bust roll even if added Heat is zero. A Cash Stash is consumed only by a bust,
changing that bust's cash loss from 25% to 10%; crew losses remain unchanged.
Fixer recovery cash is included before bust losses. Neither purchases nor support
use earn Rank. Season reset clears specialty and support with competitive state.

The content-first crew screen shows effects, prices and current slots. A selection
is a draft until its final preview and Act. Stale-resource rejection retains that
selection for a refreshed preview. Cancelling and disconnecting purchase nothing.
Action previews account for specialty, support, its consumption and actual bust
stakes. Revalidation includes both slots; purchases and consumption share the
same transaction as action resources and turn cost. Schema 5 adds two bounded
choice columns, initially empty, without changing existing player resources or
identity. Upgrades and the version marker are atomic; earlier migrations remain
unchanged. Manual stop-session and verified-backup upgrade guidance applies.

**Recon and persistent operations (issue #362, slice 6; maintainer approved).**
`[O]Ops` opens the operation slot, rival recon and private dossiers. Rival recon
costs one turn, no cash or Heat, with no bust roll or support consumption. It
settles the observed rival's elapsed resources and saves their cash and available
crew at commitment. Newcomer/raid protection does not prohibit recon and is not
removed by it. Only the observing caller can read the snapshot; its timestamp,
24-hour expiry and last-known status are always visible. This is not a live view
or a guarantee of current raid odds. Keep the latest ten distinct rival dossiers
per caller; another paid observation replaces that rival's prior snapshot. Free
inspection and raid previews use only unexpired earned intelligence. Season reset
clears dossiers, including dormant callers' data.

One PvE operation slot binds an existing fixed contract and approach when Case
commits. Case costs one turn, no cash or Heat; Prepare costs one turn and $50,
without Heat. Neither rolls outcomes nor consumes support. Execute costs one
turn and uses the ordinary approach's Heat/failure rules, including specialties,
Burner Kits and Cash Stashes. Success odds gain 15 percentage points, capped at
90%; success payout range endpoints double and success earns 30 Rank through a
separate monotonic operation counter. Fixer recovery on failure remains $20.

Success clears the slot. Failure returns it to cased: progress is useful, but
another Prepare ($50 and one turn) is required before retrying Execute. Abandon
is free, forfeits progress without refund, and requires its own final Act. Case,
Prepare and Execute each have a content-first preview and Act. Progress survives
visits and reconnects without forced waiting; browsing performs no random draws.
The slot and paid steps share the action transaction and stale-preview checks,
so racing sessions cannot use one preparation twice. Ordinary contract jobs remain
available independently. Season reset clears operation progress and its Rank.

Schema 6 adds operation progress/counters and a bounded indexed dossier table.
New state defaults empty; upgrades preserve existing identities, age, resources
and purchased crew/support state. Failed upgrades roll back the version and data
together. Backup/restore includes progress and dossiers; SysOp competition reset
and season advance clear them. The manual stop-sessions/verified-backup upgrade
procedure applies before activating this version.

**Short visits (issue #362, slice 6).** Direct Trade, Recruit and Job keys remain
available alongside the optional operation hub. The switchboard and hub show
the minimum budget to the next execution: a new operation needs three turns and
$50, cased needs two and $50, prepared needs one and no further upfront cash.
These are attempt budgets, not promises of success; failure still needs paid
preparation. If turns or cash are short, the saved next step stays visible and
the caller is told that progress can wait. Zero-turn callers retain free browsing
of contracts, operations and dossiers. First-visit advice includes a one-turn
Cautious contract. No setup prompt or compulsory operation blocks ordinary play.

The deterministic mixed-visit probe combines specialty training, recruitment and
saved operations within fifteen paid turns per day. It measures the actual
transaction path and reports policy, outcomes and saved stage. Passing the probe
establishes bounded turn use and reachable progression; human satisfaction,
pace and multi-day balance remain separate acceptance gates in issue #362.

**Compact action screens (issue #362, slice 3).** Target pickers, results,
rejections and season notices use terminal-height pagination as well as width
wrapping. Digits 1-9/0 choose a complete visible target entry and open its preview;
Next/Prev traverses pages and Back returns without spending. A split entry cannot
be selected until its final line is visible. Already-owned exchanges and ineligible
rivals show their reason and have no active selection key.

Raid selection uses the same stable, bounded directory batches as browsing, so all
eligible crews are reachable beyond the former random fifty-row sample. Result
pages retain every net resource change until continued or left with Back. These
paths support 80x24, 48x14 and 40x12, including long names, wide/combining text and
large resource values; manual terminal/transport validation remains slice 9.

**First visit and recovery (issue #362, slice 3).** New callers receive a short,
content-first guide to the map, action previews, trade/recruitment and free browsing;
the full rules stay in Help. The switchboard gives contextual next steps for no
turns, insufficient recruit cash, one remaining crew member, missing territory
income and high Heat. High-Heat guidance shows the cooldown until Trade adds no
bust roll; recruitment remains a no-Heat option, not an automatic recovery action.

Unavailable raid/root actions explain the refill before opening a target picker.
Recruit previews show the actual cash shortfall; empty rival worlds explain the
available non-PvP alternatives. Advice does not grant resources, spend turns,
change protection, or imply a waiting caller's snapshot updates continuously.

**Raid recovery and public intelligence (issue #362, slice 5; maintainer approved).**
Every committed raid attempt gives its target a 24-hour recovery shield against
all attackers, whether the raid succeeds or fails. Rejected or cancelled choices
spend nothing and grant no shield. Eligibility, transfer/losses, shield, receipt
and turn cost share the action transaction: racing or alternating attackers cannot
bypass recovery. Login, resource refresh and receipt acknowledgement never remove
or extend it. At expiry the ordinary eligibility rules apply again. The existing
48-hour lifetime-account newcomer shield and own-tier +/-1 bracket remain; season
reset clears competitive recovery but never renews veteran newcomer grace.

Public intelligence is handle, season Rank/tier, protection reasons and exact
UTC expiry. The dashboard also shows remaining recovery time. Available crew and
cash remain private, so raid previews explicitly describe uncertain odds and
percentage stakes. Exchange ownership, garrison and income remain public;
raid recovery does not block territory contests. Paid recon grants only the
private, timestamped and expiring snapshots defined above; public directories
continue to withhold cash and available crew.

Schema 4 adds the recovery deadline. An old last-attacker marker has no attempt
timestamp, so upgrade preserves its protection for 24 hours from upgrade against
all attackers and leaves an explanatory receipt. Empty old markers grant no new
shield. Migration is atomic and idempotent; ordinary login does not clear the
last-attacker record or recovery deadline. No service or background timer is needed.

**Economy targets and rules (issue #362, slice 5; maintainer approved).**
The full map must earn no more passive cash per day than fifteen average trades;
a player cannot receive repeat capture Rank from one exchange within a season;
recovery from a bust to three available members fits within fifteen low-risk
turns; defended territory retains the 10% minimum capture chance. These are
measurable constraints, not proof of fun or fair balance. Human multi-day
playtests remain required.

The ten hourly rates are $2, $2, $2, $3, $1, $2, $3, $2, $1 and $2 in map order:
$480/day for the entire map versus $495 expected gross from fifteen trades. A
capture attempt costs one turn and the role-specific cash price below, win or lose;
success additionally commits one available member. Ordinary recruitment costs
$75/turn; an owned Carrier Switch offers the service below. Expansion competes with
recruitment and defense for the same cash budget. Trade pays $20-$60 without
a cash prerequisite for the first three Trades of a turn-day; each later Trade in
the same turn-day lowers the top of the range by $5, to $20-$40, and the count
resets with the turns (issue #649, world schema 11). Twelve Trades in a row had
taken a first visit from $300 to $728 with no bust risk, so cash was a solved
problem from the first session even though Trade earns no Rank. Only the top
moves: the $20 minimum is what bust recovery is sized on, and the taper keeps
fifteen Trades above the map's $480. A steeper cut ($5 off both ends per Trade,
to $5-$15) was rejected because it broke both targets. After a bust resets Heat,
even minimum trade payouts fund recovery from one to three available crew within
ten turns.

A first successful capture awards 50 Rank per player/exchange/season. Holding
territory earns one Rank per six exchange-hours, combining fractional time across
holdings and visits. Transfers pay the prior owner's earned cash/control Rank in
the same transaction and retain fractional credit. Losing, withdrawing or busting
does not remove earned Rank. Offline owners are settled before standings and raid
brackets are presented; commit checks settled raid eligibility. Cash settlement
and control time share the ownership timestamp, so inspection cannot multiply
rewards. Normal season reset clears the new counters and capture IDs.

Schema 3 pays accrued income at the old stored rates before installing new rates;
control Rank starts at that boundary, without retroactive awards. Existing capture
Rank is preserved with a legacy credit of 450 points per old rewarded capture.
The old aggregate counter cannot identify every previously captured exchange and
retained receipts are not a complete ledger: players with prior capture awards
therefore receive no additional capture awards until next season, when all ten
become available again. A history receipt explains this transition. Cash, real
crew assignments, identities and account age survive, subject to ordinary overdue
season reset. All changes and the version marker commit together.

**Exchange identities (issue #362, slice 7; maintainer approved).** The existing
ten exchanges form a fixed ring in stable ID order, including the last-to-first
link. Every exchange remains attackable regardless of adjacency, tier or raid
shield. Owning either linked neighbor discounts a capture attempt by $10;
owning both does not stack. Ownership and the selected price are checked again
inside the capture transaction. The map shows real links, current owners,
assigned crew, security, effective defense, income, prices and services.

| Role | Sites / income | Capture cash / base Heat | Owner-only service |
| --- | --- | --- | --- |
| Public PBX | Two, $1/hour each | $25 / +4 | Lay Low: one turn removes up to 15 Heat, floor zero |
| Carrier Switch | Six, $2/hour each | $50 / +8 | Recruit one available member for $65 and one turn; +10 Rank |
| Warez Hub | Two, $3/hour each | $75 / +12 | Warez outlet: one turn, $30-$70 gross payout, +4 Heat; no Rank |

Owned Carrier Switches add two visible security points to capture defense.
Security is not living crew: it cannot be withdrawn, does not return to a
displaced owner and does not multiply the shared crew pool. Unclaimed sites
retain guaranteed capture; defended contests retain the 10% chance floor.
Capture Heat still applies Lookouts/Burner benefits. Services require ownership
at Act and preview their stakes. Lay Low and recruitment have no bust roll or
support consumption. The outlet uses ordinary trade bust rules: Burner and
specialty benefits do not apply; Cash Stash applies and is consumed only on a
bust. No additional passive bonus is introduced. Cash, turns, Heat, crew, Rank
and support changes share the existing action transaction.

Schema 7 assigns roles in original map order, retaining IDs, names, hourly rates,
ownership, crew, earned resources and receipts. It rejects a nonempty map with
other than ten sites without mutation. Roles and ring identity survive season
reset; ownership and competitive resources reset as before. The version marker
and role assignment commit together; shipped migrations remain unchanged.

**Neutral operators (issue #362, slice 7; maintainer approved).** Three fixed,
explicitly labeled NPC crews defend home sites at season start: Patch Panel
Society at #5 Public PBX (2 defenders), Night Relay Union at #6 Carrier Switch
(4 defenders plus its ordinary 2 security), and Spool Archive Collective at #7
Warez Hub (6 defenders). Home positions follow original stable map order. NPCs
use exchange state, never synthetic player accounts: they have no cash, income,
Rank, human standings entry, raid eligibility or available crew pool. They never
attack callers and never take a human-owned exchange.

Capturing an NPC home uses the ordinary role price, neighbor discount, Heat,
crew losses and once-per-player/exchange/season Rank award. Defense is public and
previews show exact odds, including the ordinary 10% floor. Displaced NPC guards
leave the scene; they cannot become human recruits. A captured home is an ordinary
human holding with its income and service. Withdrawing its final defender leaves
it unclaimed for 24 hours; the map shows the return deadline. If still unclaimed,
its fixed NPC crew returns with its original defenders. A human capture cancels
the pending return. Homes start defended again at season reset. Non-home sites
never receive neutral crews.

At each normal world boundary, at most three home rows are checked under the
existing write transaction. Returns require no background task, random draw or
per-missed-day replay, and cannot reroll a defense through browsing/reconnect.
A return between preview and Act rejects stale capture terms without spending.
Fixed repeatable jobs and operations remain available with no humans to raid or
unclaimed sites to take. These rules provide bounded PvE contention without
inventing human activity; they do not establish whole-season balance.

Schema 8 adds neutral occupancy and a return deadline. Upgrade places NPCs only
in their unclaimed homes, preserving every human holding, identity and resource.
Fresh worlds and rollover populate the same three homes. Deadline, occupancy,
defenders and version marker commit atomically; backups preserve them and SysOp
competition reset uses the same season boundary. Older migrations stay immutable.

**Scene and crew identity (issue #362, slice 7; maintainer approved).** A caller
chooses one of four fixed ASCII insignia: Modem `[::]`, Relay `<-->`, Signal
`=||=`, or Archive `{##}`. The default is Modem. It is a free cosmetic choice,
previewed before Act, with no turn, cash, Heat or Rank cost. It remains part of
identity through ordinary seasons and explicit competition reset. Identity also
shows the existing handle, current Rank/tier and trained specialty; no new
player-authored text is introduced.

The free `[I]Scene` screen offers insignia, three neutral-operator dossiers and
public territory bulletins. Dossiers combine fixed fictional biographies with
actual home ownership, defense and any pending return deadline. A human-held
home is described as displaced, never as an NPC takeover in progress. Cosmetic
identity, static biography and current facts must remain distinguishable.

Only committed captures, final-defender abandonment and NPC stationing create
public bulletins, in the same transaction as the underlying change. Each includes
UTC time, season and bounded sanitized names; no cash, available crew, recon,
job results or private receipts are published. Keep the latest 500 per world,
ordered by committed row ID, without age expiry. Normal rollover retains this
bounded history and labels new-season events; explicit SysOp competition reset
clears it with private receipts. Empty/old worlds get an honest empty bulletin
board: migration never fabricates historical events. NPC initial stationing and
actual returns are labeled NPC activity, with no synthetic human accounts.

Schema 9 adds the persistent insignia and bounded scene table transactionally.
Backup/restore includes both; reset retains insignia and clears scene history.
Synchronous storage rules and free, content-first paginated browsing remain.

**Season results and awards (issue #362, slice 8; maintainer approved).** Before
rollover, publish the UTC end time and the existing standings order: descending
season Rank, then ascending account ID. Award cosmetic Gold, Silver and Bronze
to the first three placements with positive Rank. Fewer than three qualifying
players means fewer medals; zero Rank never receives one. Medals grant no money,
crew, protection or other permanent gameplay advantage.

Under the existing rollover write lock, settle each existing human owner's final
territory earnings through the outgoing season's cutoff before computing final
Rank. Archive one snapshot for every player in that season, including dormant
players: account ID, historical handle (bounded to 80 sanitized characters), Rank,
placement, medal and insignia. Cash and available crew are not in the public
archive. Later renames, deletion of a live game row, resource changes or cosmetic
choices do not rewrite historical results. An orphaned exchange cannot credit a
missing player; reset still clears that stale holding.

Archive insertion, final earnings, competitive reset, neutral-home population and
active-season marker commit together. Any failure preserves the previous world.
Repeated/concurrent boundary visits must produce exactly one result per player.
Keep the latest twelve completed season numbers, deleting their result rows with
evicted headers. For skipped, unmaterialized seasons record only an inactive
header with no players or invented winners; process at most the retained twelve
headers regardless of absence length. Upgrade creates no fictional past results.

Schema 10 creates the archive tables. The current standings and dashboard publish
awards in advance; `[I]Scene / Season results` shows retained season status, cutoff,
podium and the viewing caller's placement. Archive records are snapshots, never
updated by ordinary play. Existing stop-sessions/verified-backup procedures apply.
SysOp advance/reset first settles overdue seasons at their published natural
cutoffs, then closes the current season at the operator-selected boundary
and retains cosmetic archive recognition while resetting competitive resources.
Older worlds remain operable before upgrade; unknown past results are not backfilled.

Each archived player receives an unread private crackdown receipt with season,
final Rank, placement and medal, committed with the archive/reset transaction.
The existing latest-500 receipt limit applies. Explicit SysOp competition reset
clears these receipts while preserving archived cosmetic recognition.
From `[I]Scene`, `Your season reports` lists the caller's historical results,
retained medal counts and best retained Rank/placement; `Hall of Fame` lists the
actual positive-Rank medal winners grouped by season. Both are free, paginated
views of the same latest-twelve-season archive, with historical handles and
insignia and no new scoring or gameplay advantage. Empty archives explain when
results arrive. The crackdown screen points callers back to the switchboard's
Log and Scene for the receipt and recognition; reading these views does not
acknowledge private receipts or spend turns.

During the final 48 hours, the first switchboard page starts with a compact reset
countdown. Subsequent lines explain the end time, a Cautious-job
entry point needing no rival or territory, and the expiry of competitive resources
including territory, assigned crew, training, support and all saved operation
progress (cased or prepared). Zero-Rank fresh-season
callers with unused turns see their current resources and actionable job/trade routes.
Help explains that newcomer protection follows preserved account age and is not
renewed by rollover. These cues change no payout, protection, award or reset rule.

**The War Dialer presentation contract (issue #494; supersedes the issue #362
slice 9 wording).** The door is a phosphor terminal, not a page of sentences.

*Palette.* Nine roles, truecolor as the design target, each with a deliberate
256-color index beside it rather than whatever a converter would pick:
`phosphor` `#39ff14` (your holdings, positive deltas, the scanline's glow),
`phosphor-dim` `#4e8a62` (the frame, ring links, gauge tracks), `mint` `#7dffb0` (headings,
your handle, the cursor), `amber` `#ffb000` (money and hotkeys), `cyan`
`#38d6ff` (NPC operators and neutral data), `magenta` `#ff3caa` (rival crews and
raids against you), `alarm` `#ff4d4d` (losses and bust risk), `ink` `#d7ffe9`
(values) and `grey` `#8a8fc4` (labels). A hotkey is always amber and bold. An
exchange's owner color is the same on the ring, in the table and in the feed.
Chrome never shares a color with content.

The label role is the one that must not be drawn from the screen's own hue
(issue #519). It was `#7f9a8c` -- 256-index 108, which is `#87af87`, sage green
-- so labels were specified as a green to sit on a green screen and vanished
into it: measured on the switchboard, phosphor, phosphor-dim, mint, ink and
that label color were 78.5% of visible characters in one narrow band. A cool
slate is off-hue from every green and clear of the established meanings of
amber, cyan, magenta and alarm.

The frame is chrome, and chrome wears `phosphor-dim` (issue #519, the second
half). It was drawn in `phosphor` -- the same color as a holding or a gain,
and the brightest green on the screen -- so the border was 36.7% of the
switchboard's visible characters and the rule that chrome never shares a color
with content did not hold for the frame itself. `phosphor-dim` was retuned from
`#1f7a3f`, a dark fully-saturated green, to a desaturated mid green so a border
is legible but recedes; `phosphor` now means only what a caller reads. The
scanline still fades mint to phosphor to phosphor-dim, and the ring's links and
every gauge's empty half stay in the chrome role they always had. The
switchboard before and after is kept as
`docs/images/door-chrome-519-war-dialer-switchboard.png`, and the four chrome
colors side by side as `door-chrome-519-swatch.png`.

*Glyph vocabulary.* Frames `┌─┐ │ └─┘ ├─┤`; owner nodes `◆ ◈ ◉ ◇`; crew `●○`;
turns `▮▯`; meters `█░`; the scanline's ramp `▓▒░`; sparklines `▁▂▃`; insignia
and badges `⟦ ⟧`; ring links `═ ║`; the brand `▚`; the prompt `›`. Every one has
an ASCII substitute, and the `ascii_art`/`plain` preset is the one place that
can prove none was forgotten.

Frames are **light**, not heavy (issue #517). A run of heavy `━` shows visible
gaps at the cell seams in many monospace fonts, so the border read as a failed
render rather than a box -- reported from a real terminal on which Voidrunner's
light frame is clean. The scanline draws the ramp above rather than the frame's
own glyph: it used to be a run of `━` inset two columns and joined to nothing
between two borders made of the same character, which read as a border that had
failed to draw. Block elements are full-cell by construction, so they cannot
show those seams, and a ramp fades in density as well as color -- which is the
only fade that survives the monochrome preset, where a role returns no SGR at
all and the old three-band version was perfectly flat.

The ring closes. Its stems stand in the same columns as the nodes they join --
a node cell is three columns and a link two, so node `i` begins at `i * 5` --
rather than at the row's extreme ends, where they sat two columns past the last
node and left the loop reading as two chains with a pair of verticals floating
between them. No corner glyphs are needed: the node glyph is its own corner.

*Components.* The door carries its own copy, like every other helper in its one
self-contained file: `meter`, `pips`, `dots`, `sparkline`, `label_value`/`chip`,
`badge`, `progress_chain`, `owner_node`, `scene_map`, `table`, `feed`, `key_bar`,
`compose` and `prose_rows`. Each returns *styled* rows. The frame leaves a row
that already carries SGR exactly as its component built it; wrapping every row
through a plain-text flattener and coloring the whole line from outside is what
turned the game into one grey block inside a green box. Rows are composed at the
frame's own inner width, because the rows a card produces are the rows its page
budget is computed from.

*Layout.* The switchboard is a card stack, not a paragraph list: an operator card
with a rank gauge, a resources card of meters and chips, the ten exchanges as the
ring they actually are, the latest receipts as a toned feed, then orders and the
season's absolute deadlines. A card's opening rule costs a row of the same height
budget as the rows under it. Numbers are right-aligned in their column and a
table's columns start at the same display column on every row; a table chooses
which columns it can carry at the width it has, and everything a narrow terminal
gives up is on the record's own card one digit away. Action bars live outside the
frame, where the cursor waits.

*Clocks.* Every absolute instant is shown in the node's display timezone, which
the drop file supplies as an IANA name (`timezone`, door_api 2), and names the
zone it is showing -- a deadline in unlabelled local time is more ambiguous than
one marked UTC, not less. One helper converts, so a new screen cannot
reintroduce a fixed zone by copying the line above it, which is how fifteen of
them came to print UTC while the rest of the node showed local time. An absent
or unresolvable zone falls back to UTC and still renders: `zoneinfo` has no
system database on Windows and depends on the `tzdata` package, so a door that
raised there would fail on a healthy node. Relative durations carry no zone and
are unaffected.

*Motion.* This replaces "there are no animation delays". Reveals and the carrier
sweep that plays while a committed result comes back are in, under three hard
limits: any key skips whatever is playing, Fast mode and the monochrome/plain
presets omit it entirely, and nothing animates between a caller's decision and
the commit -- a result is written to the database first and only then revealed.
Motion is forward-only except for one row it rewrites in place and owns, so the
screen a caller is left looking at is identical whether motion played, was
skipped, or was never enabled.

*Launch splash.* The one exception to forward-only motion is the splash a
session opens with: the call dialing out, the handshake, `CONNECT`, the
masthead burning in and the ring of ten exchanges lighting in its holders'
colors, drawn by cursor-addressed cell updates for about two and a half
seconds and then cleared, so it leaves nothing behind on the screens that
follow. Any key ends it and is consumed whole; it is omitted under every preset
that omits motion, and whenever input is already waiting or stdin cannot be
polled without reading it, so it never takes a keystroke meant for a later
screen. A frame is written at least every 50 ms until the hand-over: the splash
is never silent, so a driver that waits for output to settle does not type into
it.

*Presets.* Scene offers a free Display screen with immediate ASCII-decoration,
monochrome and Fast-mode toggles. Back writes nothing. Store one bounded boolean
preference object per caller in world metadata; preserve it across season and
competition resets and include it in the world backup. No competitive state or
archive row is modified. ASCII mode substitutes for every glyph in the vocabulary
while preserving caller names; monochrome removes color at the source -- a role
returns no SGR at all -- while retaining the screen controls the terminal UI
needs. Every status, stake and outcome is readable without color. Fast mode is
the one deliberately unframed layout: it omits optional art, flavour and motion
and keeps every stake and net delta, and its title row carries the page counter
the border would otherwise hold. Normal action results may add a short fictional
vignette. War Dialer reads the optional boolean `unicode_style` from the launch
metadata every door receives (§6, the `door_info.json` contract). False
defaults to ASCII decorations; true or omission preserves the rich default. An
explicit in-game ASCII choice wins. Changing monochrome/Fast alone does not
freeze the inherited Unicode default. The native-door JSON boundary and
supervision are otherwise unchanged.

*Review.* A screen is reviewed by looking at it. `scripts/door_gallery.py
war_dialer` renders every screen at 80x24, 64x20 and 40x12 in every preset, and
a change to a screen comes with that page attached. The suite can assert that
color reaches every body row, that hotkey, label, value and frame are four
different colors, that an exchange reads the same color everywhere, and that a
table's columns do not wander -- what it can never assert is that a screen is
worth looking at, which is why the pictures are required.

**Screen framing and one hotkey style (issue #487; extended by #494).** Every
screen under the masthead -- the switchboard, the help and first-visit text, the
event log and the record picker -- draws its body inside the door's frame, with
its title in the top border and the action bar outside, below it. The switchboard
overhaul had left the masthead framed and everything under it an unindented wall
of rows. The frame costs two rows and four columns, charged to each screen's own
page budget, and is dropped only in Fast mode, which is deliberately text-only:
there is no narrower terminal to drop it for, since 40x12 is the floor (issue
#495). One frame holds a stack of cards: the screen's title goes in the top
border and each card after the first is opened by a `├─ HEADING ─┤` rule, which
costs a row of the same budget as the rows under it. The border's right-hand end
carries the page counter first -- always spelled `page N/M`, and the handle a
scripted walk uses to know whether there is another page -- and then whatever
else the screen wants to say, for as long as it fits whole; a counter cut in half
tells a caller nothing, so the screen's own name is the half that truncates.
Hotkeys are written `[K] Label` everywhere the door prints, the rule Voidrunner
adopted in issue #400: the key is not always the label's first letter
(`[E] Map`, `[X] Root`), so that is the only spelling that carries every case.
The switchboard's action bar is laid out on a grid rather than hand-typed or
packed to the width (issue #517): every cell begins a column, so the keys line
up down the screen instead of landing wherever the previous label happened to
end. Columns are sized to their own contents, not to one global cell -- a single
wide label among short ones would otherwise pad every column to its width. Full
labels first, short labels when the full ones would not leave the page a row to
stand on, and full labels kept when shortening would not actually save a row:
spending the labels *and* the row is the worst of both. Every key keeps a name at
every supported size; the keys-only tier below that went with the terminals it
was for (issue #495). A key is never dropped. Alignment costs a row where packing
fit one more entry per line, and that row comes out of the page's content budget:
fifteen entries with full labels need eight columns to fit two rows, and eight
columns of their own widths do not fit eighty. Paging shares the switchboard's prompt row rather than its action bar,
which is already four rows of a twelve-row terminal. A bar written with
`out_prompt` leaves its row unterminated on purpose, so whatever reads it has to
close that row before the next screen draws; the first visit every caller saw had
printed the Back bar and the switchboard's own title on one row.

**Shared crew defense (issue #362, slice 5; maintainer accepted).** A player's
living crew is the available crew plus the members assigned across their owned
exchanges. Capture requires at least two available members and commits one to the
new garrison. Available crew alone determine job, raid and territory-attack
strength, and take failed-action/bust losses. Assigned crew defend only their own
exchange. Defenders displaced by capture return to their owner's available pool
in the same transaction as ownership transfer and earned income. This gives an
offline loser a recovery resource without duplicating members or raising Rank.

`[G]arrison` is a content-first owned-exchange picker followed by bounded crew
transfer choices and an explicit action preview. Reinforcement/withdrawal costs
one ordinary turn, no cash or Heat, and earns no Rank. One member must remain
available. Withdrawing the last defender abandons ownership after paying earned
income; it cannot leave a free, unstaffed income source. Capture awards are once
per player/exchange per season, including recaptures
after another owner or voluntary abandonment. The persistent per-player list has
at most ten exchange IDs; only a successful first capture adds an ID.
Previews show the actual Rank award, both pools,
the resulting defense and abandonment. Commit rechecks resources, season and
ownership. Back/disconnect spends nothing. The dashboard shows available and
assigned crew separately; action results distinguish transfers from losses.
Voluntary transfer receipts are retained as read history, avoiding an unread
self-notification; incoming rival and upgrade receipts remain unread.

Version 2 converts legacy copied garrisons atomically, after any normal overdue
season reset. For each remaining owner, preserve the real crew total, reserve one
available member, and retain as many holdings as can receive at least one defender.
Priority is descending hourly income then exchange ID. Divide the remaining crew
budget evenly among retained holdings, giving remainder members in that order.
Pay earned income before releasing unstaffable holdings and leave a history
receipt with assigned/available and released counts. Cash, monotonic Rank, IDs,
handles and account age survive conversion; no copied garrison becomes a recruit.
The version marker and all conversion changes share one transaction. Old processes
must be stopped and a verified backup taken before activation. Host ownership and
maintenance checks precede conversion. Defense alone does not establish balanced play.

**World paths (issue #362, slice 4).** The native runtime supplies the bundled
War Dialer with `<resolved-node-db-filename>.doors/war-dialer.db` beside that node
database. This derives from the persistent database locator, never mutable node
or door display names. Distinct database paths get distinct defaults; renaming or
relocating the node database requires moving its companion world or an explicit
override. Standalone execution keeps the legacy home-directory default.

A profile's `WAR_DIALER_DB_PATH` overrides the process setting, which overrides
the node default. Overrides expand to absolute paths before entering the door's
temporary/installation cwd. Only the bundled entrypoint/module or an explicit
profile override receives this setting; the full parent environment stays private.
SysOps can inspect the effective path through Compatibility setup / Check setup.

When the node default is absent and a legacy home-directory world exists, launch
requires explicit migration or override rather than silently replacing player data.
Migration is manual, with stopped sessions, a SQLite-consistent backup and verified
node-local user-ID ownership. An explicit override is an operator choice and must
not point independent nodes at the same world.

**World ownership and node backup.** First host launch binds the world to an opaque
random namespace retained in the node database and forwarded only in War Dialer
metadata. Subsequent launches must match it; a bound world cannot be entered as a
standalone Guest. Legacy adoption requires the documented manual user-ID check.
Backups preserve the node namespace with all discovered existing worlds: the
node-default companion and registered profile/process overrides, deduplicated by
resolved path. A world component contains SQLite-consistent snapshots, supported
schema versions and checksums, with a limit of 64 worlds and 512 MiB per world.

Backup rejects active game sessions; restore requires stopped node services,
explicit destinations for every archived world and the paired node database.
Read-only verification checks schema, integrity and namespace ownership. World
staging/rollback stays on each destination filesystem; old WAL/SHM/journal files
participate in the switch and rollback. A stable SQLite session-guard sidecar
holds shared read leases during play and an exclusive lease during maintenance;
process exit releases it without deleting its inode. Restore retains the prior
generation and journals unresolved failures. Service/profile path activation and
old-binary exclusion are manual SysOp actions, as documented in the door guide.

**SysOp competition controls.** The SysOp console (a War Dialer door's
**[W]orld** screen) and the local CLI provide read-only status, persistent
maintenance and confirmed season/reset operations. Maintenance excludes new
callers and cannot be changed over an active game session. Destructive controls
require maintenance on, an exact-filename confirmation, a reason, and a freshly
created and verified complete node backup. The CLI additionally requires a
stopped node; the console does not (issue #726, the maintainer's decision), since
maintenance already keeps callers out, the world's session guard refuses anyone
still inside, and the backup is the same live backup the console's Backup screen
takes. The console needs a live node for the identity directory that backup
captures, and records the SysOp account in the world's audit. They use the normal
atomic rollover and advance the season number. A reset additionally clears
receipts; the maintainer selected preservation of player identities and account
age, so reset never renews newcomer protection. Ownership and the latest 100 SysOp
audit entries survive, with destructive changes and their audit record in the same
transaction. Maintenance stays on until the SysOp explicitly reopens the world.

Host metadata is either absent (an intentional unbound standalone demo) or valid.
Unreadable, oversized, malformed or invalid identity/dimension metadata is refused
before world creation; it never falls back to Guest. Caller messages remain clear
and SysOp diagnostics are bounded. Commands do not activate or redeploy services.

**World schema compatibility.** SQLite `user_version=1` identifies the original
War Dialer schema; the current version is 11. `user_version=2` added shared-crew
resource semantics without
changing its column layout. A complete unversioned world is adopted through a
numbered migration; its additive fields, retained history and version marker
commit together or roll back together. New empty worlds use the same migration.
Future schema versions, incomplete/unrelated layouts and failed SQLite integrity
checks are refused before initialization or journal-mode changes. Startup never
replaces an unreadable world with an empty one. Recovery preserves the original;
use compatible game code or a verified backup, with sessions stopped.

### Issue #168 — real-time relay for Link direct chat

**Goal:** decide between the two structurally different designs the issue
itself poses for live (Noise XX) relay between two mutually-unreachable
(outgoing-only-to-each-other) nodes: double-hop relay-as-participant
(the relay terminates one Noise session per leg and re-encrypts between
them) versus a raw-socket/TCP-level proxy below the Noise layer (the
relay blindly forwards bytes, never touching the handshake or its keys).
Not a newly-discovered gap — the design doc already named this as a
deferred "separate future protocol" (§8.10); this issue is that design
pass.

**Context that made this tractable now, not a decision in itself:** a
second, related idea is in discussion — turning ReLink (the project's
own persistent, internet-reachable test node) into a stable, always-up
default relay so a home SysOp who can't or won't expose a port has a
frictionless path onto the mesh, without that being a hard dependency
(any other willing peer, or a commercial provider for the parallel
managed-DNS idea under #201, works exactly as well — nothing forces
ReLink specifically). That product/infrastructure question is real but
still early and not decided here. What it *does* settle is the async-
relay model's own objection to the raw-proxy design: `relay_selection.py`'s
reliability-ranking machinery exists to route around relays that might
disappear, and a relay explicitly committed to staying up removes the
need to solve general reliability-ranked live-relay discovery before a
v1 can ship. The protocol decision below stands on its own regardless of
who ends up operating such a relay.

**Decision 1 (locked in) — raw-socket/TCP-level proxy, not double-hop.**
The deciding factor: raw-proxy requires **zero changes** to the already-
shipped `LinkRealtimeSession`/Noise XX handshake code. Two directly-
handshaked endpoints run the exact same mutual authentication they'd run
if actually adjacent; the relay is as invisible to Noise as any ordinary
router hop, since it never participates in the Diffie-Hellman exchange
and structurally cannot decrypt anything. That confines all new code to
connection setup (a small rendezvous exchange — "I want to reach
fingerprint X" / "I'm X, waiting"), not the confidentiality-critical
path itself, and it is a well-understood pattern elsewhere (a TURN
server, an SSH jump host, Tailscale's DERP relays), not a novel design.

A real alternative was considered and rejected for now, not dismissed:
double-hop can be built as a *hybrid* where the relay stays a genuine
protocol participant for control-plane frames (subscribe/presence/ping)
it's fine to see, while chat-content frames carry their own additional
encryption hop the relay can't read — giving real per-frame-type abuse
mitigation (raw-proxy can only see bytes/timing, never structure) on top
of content confidentiality. Rejected for v1 because it's solving a
structural-abuse-mitigation problem with no evidence yet that it's
needed, at real cryptographic-design cost paid up front: a second key-
exchange scheme layered inside the double-hop transport, a new session
shape (today's `LinkRealtimeSession` assumes exactly one remote
fingerprint, not a triangulated A-relay-B relationship), and a new frame
family. If frame-level abuse mitigation becomes a real operational
problem later, the hybrid design is the documented answer to revisit —
not re-derived from scratch.

**Trade-off stated plainly:** raw-proxy does not get either design to
zero metadata exposure — the relay still learns which two fingerprints
talked, for how long, and roughly how much traffic, same as the hybrid
double-hop's control-plane visibility would show. Raw-proxy also doesn't
plug into `relay_selection.py`'s existing reliability-ranking/consent
model at all — it's a structurally different kind of "relay" than the
async store-and-forward one, sharing a name but no code or selection
mechanism. If a fully decentralized *marketplace* of live relays (not
one well-known anchor) is ever wanted, that discovery/ranking layer
would need to be built fresh for this model rather than reusing the
async one — an accepted, deferred cost, not an oversight.

**Decision 2 (locked in) — bounded resource limits for live relay.** Modeled
on, but distinct from, the two existing bound families this touches: each
leg of a bridge is an ordinary `LinkRealtimeSession` (`netbbs.link.
transport`), already governed by its own `REALTIME_DEFAULT_*` bounds
(64-frame outbound queue, 100 frames per 10s window, 45s heartbeat lease,
5 protocol strikes); the async relay mailbox (`relay_mailbox.py`) already
bounds *its* resource at 50 envelopes per recipient. Neither transfers
as-is, because raw-proxy's actual cost shape is different from both:

- **Concurrent bridged-pair limit.** A relay running one bridge is a real
  third participant holding *two* live Noise sessions (one to each chat
  party) for that pair's entire conversation — meaningfully more
  standing cost per pair than either a single direct session or an
  at-rest mailbox entry. A new node-level `max_concurrent_relayed_pairs`
  limit (SysOp-configurable, same `nodeconfig.py` dataclass-field-plus-
  validation shape `ShutdownConfig` already uses) caps this directly,
  independent of the per-session frame-rate bound above, which caps a
  *single* session's chattiness, not how many a relay carries at once.
- **Per-pair byte-rate bound, not a frame-rate one.** `max_frames_per_
  window` counts discrete parsed frames — meaningless for raw-proxied
  bytes, which the relay by design never frames or parses at all. A
  bridge needs a bytes/second ceiling instead; exceeding it closes the
  bridge, the same "drop rather than silently degrade" precedent
  `LinkRealtimeSession.send()`'s existing slow-consumer handling already
  sets for a full outbound queue.
- **Idle-bridge timeout is protocol-agnostic, unlike the existing
  heartbeat lease.** `LinkRealtimeSession`'s own dead-peer detection
  inspects real ping/pong frames — raw-proxy structurally cannot do
  that, since the relay never sees frame semantics, only opaque
  ciphertext bytes. A bridge instead needs a dumb "zero bytes observed
  in either direction for N seconds" timer: the two actual endpoints'
  own (relay-invisible, encrypted) heartbeat traffic keeps a genuinely
  live bridge from ever tripping it, with no frame-aware logic required
  on the relay's side at all.
- **Bounded, timed-out pending-rendezvous table.** A node that shows up
  first, before its counterpart, waits — bounded in count (a cap on
  simultaneous pending requests per relay, the same "bound remotely
  influenced resources" principle `MAX_MAILBOX_ENVELOPES_PER_RECIPIENT`
  already applies to the async mailbox) and in time (a lone request
  that waits past its own timeout expires and is reported back to the
  requester as an explicit failure — CLAUDE.md's "fail clearly," not a
  request silently forgotten).
- **The existing slow-consumer-drops-the-session behavior composes
  across two hops for free, unneeding any new logic of its own**: each
  leg is its own ordinary `LinkRealtimeSession` with its own existing
  bound: a slow leg drops only that one session exactly as today, and
  the relay tears down the other leg in response (a bridge with only
  one live end isn't a bridge) — no bespoke two-hop-aware queue ever
  needs writing.

**Decision 3 (locked in) — v1 fallback UX for two mutually-unreachable
nodes.** Extends this project's own already-shipped local convention
rather than inventing a new one: `netbbs.net.chat_flow`'s `/msg`/
`/private` already require the recipient currently online, refusing
plainly ("X is not currently online.") when they aren't, with local mail
as the standing async alternative. The cross-node case gets the identical
shape: attempting live Link direct chat with a peer whose node can't
currently be bridged (no relay reachable, the relay's own pending-
rendezvous request times out, or the concurrent-pair cap above is full)
produces an explicit, never-silent refusal naming the situation plainly
(e.g. "<user> can't be reached for live chat right now.") and points at
Link mail (`link_message`/`relay_mailbox.py`, the *already-shipped* async
cross-node messaging path — not a new mechanism) as the immediate
alternative. The caller-facing message deliberately does not distinguish
*which* of the possible reasons applied — offline peer, no relay, relay
at capacity, rendezvous timeout — mirroring this project's existing
stance elsewhere (design doc §12) that such operational detail about a
*remote* node is not something a caller needs and could leak more than
intended about the other side's situation.

**Decision 4 (locked in) — rendezvous frame shape.** New frame types
added to the existing `REALTIME_FRAME_TYPES` set (`netbbs.link.
protocol`), not a new protocol version or frame family: extending that
frozenset without bumping `REALTIME_PROTOCOL_VERSION` is already this
file's own established pattern (e.g. the presence frames joined the
original subscribe/channel_message set the same way), and an old peer
encountering an unrecognized new type already fails cleanly via the
existing "unsupported real-time frame type" rejection — no separate
negotiation needed. Rides over the *requesting* node's own already-
authenticated `LinkRealtimeSession` to the relay (the ordinary session
that already exists from `relaying_for`/relay-consent setup) — no new
authentication mechanism, matching raw-proxy's own core premise of
confining new code to connection setup, never the confidentiality-
critical path:

- `relay_request` `{target_fingerprint}` — sent by either party wanting
  to reach the other through this relay.
- `relay_waiting` — the relay's reply when only this side has shown up
  so far (bounded by the pending-rendezvous timeout, Decision 2).
- `relay_ready` — sent to *both* sides once the counterpart has also
  shown up; raw-proxy byte-pumping between the two begins immediately
  after.
- `relay_reject` `{reason}` — the relay declines outright (pending-table
  full, no `relaying_for` relationship covering this pair, concurrent-
  pair cap reached) — explicit, matching Decision 3's fail-clearly
  requirement, never a silent drop.

This closes every acceptance criterion issue #168 named.

**Implemented** (§8.10.3 is the normative description): `netbbs.link.
realtime_relay` (relay server half and party half), `netbbs.link.
realtime_direct` (session establishment order, direct messages, the
reliable-node anchor connectors), `netbbs.net.link_direct` (the `/msg
user@node` flow and the receiving-side deliverer), five `[link]
live_relay_*` bounds with the defaults §8.10.3 lists, and a `relay_ready`
payload that names the relay's attach address so a party never has to
remember which address it reached the relay at. Two implementation
choices beyond the four decisions: the invitation reuses the
`relay_request` shape with the invitee's own fingerprint as target
(no fifth frame type), and a bridge is only ever offered between two
nodes both currently connected to the relay -- consent is the standing
session, so the asynchronous `relaying_for` model is not consulted.

### Issue #201 — managed netbbs.org subdomain + dynamic DNS — closed

**Goal:** since the project controls the `netbbs.org` domain, offer SysOps
an easy way to publish their board under it (e.g. `myboard.netbbs.org`)
and, for boards on residential/dynamic IPs, keep that record pointed at
the node's current address without manual DNS maintenance. Two
components, not one: a one-time subdomain *registration* (name
reservation + initial DNS record), and a recurring *updater* that keeps
the node in contact with the service and, for a board that asks for it,
pushes a record update when the node's public address changes. A board
on a static IP still wants the friendly subdomain without the address
tracking, and that is the `dynamic` choice at registration; what it
cannot opt out of is the contact itself, which is the liveness signal
every managed name owes the service (Decision 10 — this paragraph used
to call the updater optional, and issue #600 found the code had never
agreed).

**Decision 1 (locked in) — offered via a prominent prompt on first-SysOp
bootstrap or first authenticated SysOp login, not a silent default and
not a toggle a SysOp has to go discover.** Two different concerns were
in tension here and both are real: a bare opt-in-only design loses most
of the feature's actual value (the whole pitch is removing first-run
friction — a setting nobody discovers might as well not exist), but a
silent default also isn't right, because this makes the node contact and
register public presence with project infrastructure before the SysOp
has decided whether they want that at all — a private/test node would
get unexpectedly enrolled. This is the same *kind* of decision as Link
participation itself, which already isn't automatic on a fresh node
(seeds must be configured before a node reaches out and joins the mesh)
— for internal consistency, "does my node touch external infrastructure
and become discoverable" should stay an explicit choice here too, just
asked at the moment it matters instead of requiring discovery later.
Code review follow-up (PR #218): a supported persistent deployment
bootstraps its first SysOp via `netbbs.admin` and then runs headlessly
under systemd/rc.d — a literal "first daemon run" prompt has no
interactive input channel at that point and would either block startup
or silently be skipped. The prompt is anchored to an existing
interactive surface instead — first-SysOp bootstrap, or that SysOp's
first authenticated login if bootstrap itself stays a non-interactive
CLI invocation — with the accept/decline answer persisted so it is
asked exactly once, not on every subsequent login.

*Default flipped by issue #219 Decision 7:* on the shared first-run screen the
prompt's bare-Enter default is now accept (both first-run choices are pre-set
to accept so accepting everything is two keystrokes); an explicit "n" still
declines, and the decision is recorded once either way.

*The accept and the registration are separable in time (issue #634).*
Accepting continues straight into choosing a name, because an opt-in
that gets the SysOp nothing without a second trip through the console
loses the friction argument above. But the earlier of the two anchors —
first-SysOp bootstrap in `netbbs.admin` — runs before the node has ever
started, and the fingerprint the service knows a node by is cached by
the node's own startup; `netbbs.admin` is given a database path, not the
node's configuration, so it cannot load or create the identity itself
without risking a second one in the wrong directory. On such a node the
accept is recorded as always, the SysOp is told the name is picked once
the node has started, and the registration is *owed*: the next
interactive surface that can register — a SysOp's authenticated login,
or `netbbs.admin` run again, which is the only one a headless deployment
ever uses — opens the name editor, once, exactly as the prompt would
have. Backing out of it is a final answer, like declining the prompt;
registering by any route settles it; the DNS screen's `[R]egister` is
the same editor from then on.

**Decision 2 (locked in) — the managed-service credential is a separate,
auto-generated, per-registration secret, not the node's own Ed25519 key.**
Both the self-hosted path (SysOp supplies their own dynamic-DNS
provider's credentials, no design question) and the managed path stay
available, not exclusive. For the managed path specifically: reusing the
node's existing Ed25519 key (the Link protocol trust root — Noise XX
handshake identity, canonical event signing) was considered and
rejected. The convenience argument for reuse doesn't actually hold — a
minted credential, generated at registration and stored transparently by
the node, gives the identical zero-manual-handling experience reuse
would. What reuse *would* cost for no real gain: blast-radius coupling
(a compromised node key would also hijack the SysOp's public DNS name,
not just Link identity) and coupling any future node-key rotation/
recovery work to also remember to propagate to DNS. A separate
credential keeps the two systems', and their compromise/recovery
stories, fully independent.

**Decision 3 (locked in) — name governance is first-come-first-served
plus a reserved-word blocklist and a one-name-per-node cap; a registered
name only actually goes live once the node has maintained a minimum age
of successful contact with the registration service; new registrations
service-wide are rate-limited, rejected outright once that rate is
exceeded; and total active managed registrations are capped by a
separate cumulative ceiling, refused once reached — no preventive
identity vetting, and no human review queue, beyond that.** Matches how the project
treats registration/content elsewhere (SysOp owns the trust decision,
best-effort not gatekept) — requiring identity *verification* before
registering a subdomain would add real friction against the feature's
own point, for a comparatively low-stakes resource. The blocklist covers
only the obvious cases (the project's own names, trademarks, slurs) —
not a general dispute-avoidance mechanism.

Code review follow-up (PR #218): first-come-first-served plus a
blocklist bounds *which* names can be taken but not *how many* —
without a cap, one node could hold indefinitely many names, consuming
DNS-provider records/cost and squatting desirable ones. A first fix
attempt (a bare cap of one name per node/registration-credential) was
itself found insufficient on further Codex review (PR #221): a node
identity and a registration credential (Decision 2) are both free for a
remote client to mint — an attacker can generate a fresh node identity
per desired hostname and hold all of them simultaneously, each
individually satisfying a "one-per-node" cap.

**A second fix attempt kept the one-per-node cap and added a minimum-age
gate** on when a reservation actually becomes a live DNS record,
deliberately decoupled from Decision 1's first-run opt-in: accepting the
offer at first run (or first SysOp login) still costs nothing and
happens immediately, but the node's registration *intent* is recorded,
not yet published, until the node has maintained a minimum period of
successful contact with the registration service. **This was itself
found insufficient on yet another Codex review, same PR (#223):** the
gate only costs an attacker *wall-clock time*, not *effort per
identity* — a single process can run a trivial heartbeat for thousands
of fake node identities in parallel, all maturing simultaneously, at
essentially zero marginal cost per additional name. The gate delays the
attack once; it doesn't bound how many names come out the other end of
that delay, so the DNS-provider cost/squatting problem this decision
exists to prevent recurs in full once the qualifying period passes.

**The age-gate is kept anyway, as a mild friction layer, but the actual
bound is a separate, service-enforced admission control layered on
top: a rate limit on new registrations across the whole managed
service (not per node/identity — the thing an attacker cannot multiply
by minting more identities), automatic up to that rate, with anything
beyond it originally specified as queued for the project maintainer to
review** — the same "a human reviews it" shape Decision 4 already uses
for contested-name disputes, extended from content disputes to
registration *volume*. This is what actually closes the gap:
DNS-provider record cost and squatting are bounded by how fast the
*service* will create new records at all, independent of how many
identities a single attacker can mint and age in parallel.

Two further Codex findings, same PR (#225), both about resources a
review queue would itself introduce or leave unbounded, not about the
rate limit's own logic: the queue would need its own explicit-capacity
bound (itself a remotely-influenced resource a Sybil attacker submitting
past the rate limit could otherwise fill without limit); and separately,
a rate limit alone bounds *speed*, not *total count* — an attacker
patient enough to always submit exactly at (never over) the threshold,
keeping every registration's contact alive indefinitely so nothing
qualifies as abandoned, could still accumulate an unbounded number of
active records over a long enough time. The second finding needed its
own fix regardless of the queue question: a separate cumulative cap on
total *active* managed registrations service-wide, refused (not queued)
once reached, freed only as existing registrations are voluntarily
released or genuinely abandoned (Decision 5) — the rate limit alone
never stands in for a real total ceiling.

**The review queue itself was dropped entirely during implementation
planning, not carried forward as a bounded-capacity queue.** Once a
request is queued at all, the realistic resolution is the same either
way: the maintainer hears the SysOp's explanation and decides by hand.
A capacity-bounded queue adds real code and a genuine single point of
(human) failure without actually simplifying that manual conversation —
the same outcome is reached faster, with less to maintain, by simply
rejecting outright once the rate limit or the cumulative cap is
exceeded, symmetric with each other, and telling the caller plainly
that the service is at capacity with a contact channel for the rare
legitimate exception. Both are hard-reject and service-wide. A reclaim
(Decision 5) bypasses the *rate* limiter because it is not a new
registration, but it must still fit the cumulative active-registration
ceiling and the one-active-name-per-node limit: reactivation consumes a
real active slot, and release/reclaim cycling must not create more live
rows than either bound permits.

*Issue #598:* for three releases neither refusal carried a channel, and
the cumulative-cap refusal said "try again later", which is wrong for
that cap specifically — the rate limiter refills, but the ceiling frees a
slot only when some other registration is released or swept as
abandoned, so a SysOp told to wait retries against a wall instead of
getting in touch. The channel is the operator's own setting
(`MANAGED_DNS_CONTACT`, `services/managed_dns/README.md` §4), not a
project address baked into a service anyone may self-host; an instance
without one says so in words. The cap refusal now says what frees a
slot and that retrying will not; the rate-limit refusal keeps "try again
shortly", which is true. The node shows the service's sentence, not the
JSON it arrived in.

*Two things this decision states as flat rules that the rename workflow
(Decision 9) deliberately bends (issue #602).* The one-name-per-node cap
is one **except across a rename**, where a node holds two active rows —
the live name and its maturing replacement — until the replacement
matures; make-before-break requires exactly that, and it is bounded at
two (the node refuses a second rename while one is pending, and the
service treats a retry against an existing `replaces_name` link as
recovery of the same replacement, never a third row). And a rename
**spends a rate-limit token** where a reclaim does not: a reclaim
reactivates a row and a record that already exist, whereas a rename
reserves a new name and will create a new DNS record, which is precisely
the cost the limiter exists to bound. Both were accidents of
implementation until written here; both are now the rule.

**Decision 4 (locked in) — contested-name disputes are manual and
complaint-driven, stated as such, not implied automation; the operator's
end of that process is a revocation which takes the name away from the
registrant who held it, and nothing more.** At this project's current
scale, there is no realistic alternative to a human (the project
maintainer) reviewing a reported impersonation/abuse claim and revoking
if warranted. Documented explicitly so this isn't mistaken for a more
automated process than actually exists.

Issue #599: for three releases that was the whole of it. The decision
said a human revokes, and nothing existed for a human to revoke *with* —
no channel a complaint could arrive through, no endpoint or command, and
no runbook step. Doing it by hand meant editing the service's SQLite and
deleting the record out of BIND separately, with nothing to stop the
holder's node republishing on its next heartbeat. A decision whose
enforcement is entirely manual still has to say what the manual act is.

**The act is `POST /admin/revoke`, on the running service, gated by a
bearer token that is unset by default.** Inside the service rather than
a separate CLI because it must share the same transition lane as the
heartbeat, the sweep and every SysOp-driven transition — a second
process editing the same database could commit from a snapshot the
sweep had already moved past. Unset by default because a public-facing
service should not carry an administrative route that merely hopes
nobody finds it; an instance whose operator has not configured a token
has no administrative surface at all, and the refusal is identical for
"no token configured", "wrong token" and "no header" so the response
cannot be used to learn whether this instance has one.

**A revoked name is a fourth terminal status, not a reuse of
`released`.** The difference that matters is reclaim: a released or
abandoned row is deliberately reclaimable by the credential that held
it, for the length of the cooldown, which is Decision 5's whole point.
Applied to a takedown that same rule would undo it — the registrant's
node still holds the credential, and its registration draft prefills the
name it just lost, so a reclaim is one keystroke. A revoked row is
reclaimable by nothing.

**It expires on the same cooldown as the other two exits.** Revocation
blocks the registrant who was taken down, not the name forever: once
`released_at` ages past the shared cooldown the name is available to a
genuinely new registrant, including in principle the person it was taken
from. Permanent retirement was considered and rejected for the same
reason Decision 5 rejected it for voluntary release — an ever-growing
set of names retired for the life of the project — and because a
mechanism for names nobody may ever hold already exists: the
`blocklist` module's `RESERVED_NAMES`, curated by hand and changed by a
code change, which is where a permanently-barred name belongs.

**Revoking one half of a rename takes both halves.** A rename is the one
state in which a single registrant holds two names (see Decision 3's own
note on the per-node cap), so revoking the live name and leaving the
replacement to mature would hand the taken-down registrant a working
name. The voluntary release path refuses in this state and tells the
SysOp to cancel the rename first; an operator acting on a complaint has
nobody to ask, so revocation takes both rather than refusing.

**Only the rows that are still this registrant's, and still the rows
that were checked.** A replacement can outlive the name it replaced, so
the same node-fingerprint check rename completion and cancellation apply
before mutating anything through a `replaces_name` link applies here
too: a name whose cooldown elapsed and was reissued to a different node
is never taken by a complaint about the node that used to hold it. And
because a fresh registration does not pass through the transition lane
that a revocation holds, each row is re-read after the provider awaits
and written only if its credential hash is unchanged — proof it is still
the registration that was checked, rather than one that claimed the name
in between.

**Publication is undone before the rows move, and a provider failure
revokes nothing** — the same rule voluntary release already follows.
A takedown recorded in the database while the record is still resolving
would be worse than no takedown, because it looks finished. Only a row
which can still hold a published record is worth a provider call at all:
release and abandonment leave `last_known_address` behind on a row whose
record they already deleted, and failing a revocation on a deletion that
had nothing to delete would leave the credential reclaimable during
exactly the provider outage an operator cannot wait out.

**The registrant is told that, and where to write — not why.** The
first cut of this left the taken-down SysOp with a 401 the node read as
`abandoned`, and a `[R]egister` refusal that spoke of a cooldown: their
board went dark under a badge that blamed their own uptime. The service's
uniform "unknown or inactive registration" exists so that a caller
presenting a stale or invented credential learns nothing; the credential
that *held* a revoked name proves the caller is the one person the fact
belongs to, and the only thing the answer reveals is their own
registration's state. So every credential-bearing route — heartbeat,
release, rename, cancellation, and a reclaim by either path — answers a
revoked credential with `status: revoked` and the operator's contact
channel (Decision 3's `MANAGED_DNS_CONTACT`), and the node adopts that as
a terminal state of its own: the updater stops, the DNS screen shows
REVOKED with the channel, and `[R]egister` with a different name works as
usual. The reason stays the operator's. Anyone else asking for the name
still gets the cooldown refusal and learns nothing.

**The operator acts from inside NetBBS, not from a shell on the service
host.** The node whose operator also runs the service carries
`[managed_dns] admin_token` in its `netbbs.toml` — config file only,
never a command-line flag, since a secret in `argv` is visible to every
process on the host — mirrored into the node database at startup like
`service_url`, absence included. Its presence is what makes the Managed
DNS status screen offer `[A]dminister service`: the service's whole
table (`POST /admin/registrations`, same token, same uniform refusal,
every row but its credential hash, inactive rows included because a
name inside its cooldown can still be the subject of a complaint), one
registration in full — when it was registered, whose node, when it last
checked in, what it publishes, whether a rename is in flight, the reason
if already revoked — and `[R]evoke` behind a required reason and a
type-the-name confirmation, the same shape every delete in the console
uses. That is README §8's checklist made executable, minus the `dig`,
which stays the operator's. The reason prompt before the type-the-name
confirmation is a deliberate exception to §3.5's one-value rule: the
reason is the audit record the runbook requires, and a revocation is an
action against somebody else's board.

**Reports arrive through the project's issue tracker**, which is a
public channel and a real trade-off: an impersonation complaint tends to
name the impersonated party. The alternative considered was a dedicated
address, which is better suited to the content but is infrastructure
this project does not otherwise run, and an unmonitored one would be
worse than the tracker. Nothing in the mechanism assumes a report
arrived there. `services/managed_dns/README.md` §8 carries the
operational half: what to check before revoking, what separates abuse
from an ordinary naming dispute this decision deliberately does not
cover, the request itself, and what the former holder sees afterwards.

**Decision 5 (locked in) — every exit path shares one deliberately
generous cooldown before a name becomes assignable to a *different*
registrant; this is an accepted, bounded residual risk, not a solved
one.** There are four ways a name stops being live, not the two this
decision originally named (issue #601): voluntary release, abandonment
by the sweep, completion of a rename (the old name is released the
moment the replacement's record is published — Decision 9), and
operator revocation (Decision 4). All four set the same `released_at`
and start the same timer; the first three stay reclaimable by the
credential that held the name for as long as it runs, revocation by
nothing. A SysOp planning a rename should read that as: the old name is
not free the moment the new one goes live, and not re-registerable by
them either while they hold their one name under the new label. §8.10 states that
"the remote node label, endpoint, DNS name, and TCP address are never
identity authority" — real *Link* node identity is verified by the
Noise XX handshake against the Ed25519-derived key, independent of how a
connection was dialed, so a reassigned DNS name cannot impersonate a
node at the Link protocol level; that fact is why Decision 4's manual,
complaint-driven dispute process can stay lightweight for
*impersonation* claims specifically. It does not make reassignment safe
in general: ordinary Telnet and plain-HTTP callers are never protected
by that handshake, carry plaintext passwords, and a caller who still has
the old hostname bookmarked after reassignment can have credentials
harvested by whoever holds the name now — even an HTTPS caller can be
handed a convincing fake board once the new registrant obtains a
legitimate certificate for the name.

A Codex review (PR #221) correctly pointed out that a finite cooldown
only *delays* this exposure, it doesn't bound it to zero — a caller can
in principle hold a bookmark longer than any cooldown. That observation
is true but was weighed against the wrong bar: zero residual risk isn't
the standard any real identifier-reassignment system actually meets.
Domain registries drop-catch expired names — commonly with *no* grace
period at all — and telecom carriers recycle phone numbers after a
dormancy window (typically 90 days to a couple of years), both fully
aware that a returning party can be phished by whoever holds the
identifier now; neither treats "never reassign" as the answer. A
permanent-retirement design was tried in this entry and reverted:
correct in principle, but strictly more conservative than the
registries and carriers this project is directly comparable to, and it
trades a security property nobody else in this space provides for an
unbounded, ever-growing cost (a permanently-retired name is retired for
the life of the project, not just until interest fades). **The bounded-
cooldown design is kept, deliberately set longer than commercial
practice needs to be** (on the order of 90 days, well past a typical
registrar's ~30–45-day redemption window) **since NetBBS has no
commercial pressure to recycle a name quickly and generosity here costs
nothing.** A SysOp choosing to leave stops their own renewal and DNS-
updater contact immediately; the name itself does not become claimable
by a different registrant until the cooldown elapses. Exact cooldown
length is an implementation-time parameter (see the closing note
below), not fixed here, but it is the same parameter for both exit
paths, not two.

**Settled without being a real fork:** how this interacts with existing
Link node addressing — a full peer's descriptor already advertises a
host/port (`advertised_host`/`advertised_port`) for other nodes to dial;
a stable managed hostname is exactly what belongs there instead of a raw
dynamic IP. No new addressing concept, no conflict with fingerprint-
based node identity, which stays the actual trust root regardless. This
covers only the Link-to-Link dial path, not the caller-facing address a
human dials — see Decision 6.

**Decision 6 (locked in) — the managed hostname's caller-facing address
is standard ports on a fixed, documented convention, not a new discovery
mechanism.** Code review follow-up (PR #218): an A/AAAA record alone
cannot tell a human caller which transport or port to use, and
`advertised_host`/`advertised_port` (Decision 5's "settled without being
a real fork" above) describes only the Link HTTP listener — a
Link-disabled or outgoing-only board has no dialable address there at
all, so reusing it doesn't by itself deliver on "myboard.netbbs.org" as
a caller-facing promise. Resolved by convention rather than a new
protocol: a managed subdomain implies the node's Telnet (23) and SSH
(22) listeners sit on their standard ports, the same assumption every
plain hostname-based BBS address already carries. Web is the same
convention with one added, non-optional requirement: `netbbs.net.
nodeconfig`'s own web listener provides no TLS of its own, only through
an external TLS-terminating reverse proxy, so "web (443)" specifically
means that proxy bound to 443 and forwarding to the node's loopback web
listener — never NetBBS's own listener bound to 443 directly, which
would silently serve plaintext HTTP (including password entry) on the
port every caller assumes is HTTPS (code review follow-up, PR #221).
A board that cannot or will not run on standard ports (web's TLS-proxy
requirement included) keeps its managed DNS record (useful for the
dynamic-IP-tracking half of this feature alone) but does not get a bare
`myboard.netbbs.org` caller address as part of it; publishing a
nonstandard port remains the SysOp's own responsibility to communicate,
same as today.

*The convention is advisory and unverifiable, and it is stated where the
name is (issue #603).* Neither the node nor the service can see a
port-forward, a proxy or a firewall in front of a listener, so nothing
enforces this and nothing pretends to. What the node *does* know with
certainty is its own configured ports — and NetBBS's shipped defaults
are 2323/2222/8080 on purpose, since binding below 1024 needs privilege
this process should not want, so the default node is exactly the one
this decision says gets no bare caller address. Until issue #603 the
convention lived in one help panel behind a `[W]eb behind HTTPS proxy`
field whose answer was discarded, and the status screen showed
`myboard.netbbs.org` with a LIVE badge and no port in sight. Now the
registration editor and the Managed DNS status screen both state the
convention and measure it against the listeners the node recorded at
its last startup: "SSH: this node is configured for 2222, so a caller dialling
22 needs a port-forward or proxy in front of it", "Telnet: not enabled
on this node", and for web whether `[web] public_url` names an HTTPS
front — the one statement a SysOp has already made about TLS, which
replaces the discarded question. "Cannot verify" is not "cannot
mention". The LIVE badge itself is now made only once the service has
confirmed a published record; `matured` alone reads "NOT YET PUBLISHED".

**Decision 7 (locked in, implemented) — the managed-DNS credential is
in scope for node backup/restore, as an addition to §13.4's contract,
not a separate ceremony.** Code review follow-up (PR #218): §13.4
already treats a node's recoverable state as one atomic set of specific
artifacts (database, node identity, SSH host key, banners) precisely
because a partial backup silently loses things a SysOp needs after
restoring from disk loss. The managed-DNS credential (Decision 2) is
exactly that kind of durable node state — without it, a restored node
cannot update, voluntarily release, or benefit from Decision 5's
same-owner reclaim window for its existing registration. Its credential
file (`netbbs.managed_dns.credential`) joined §13.4's backup manifest as
its thirteenth artifact, the same plain-file-copy handling already used
for node identity and the SSH host key — see §13.4's own table.

**Decision 8 (locked in) — a node learns the managed service's address
from a shipped constant, overridable per node; it is not waiting on an
instruction from an operator there was never a way to give.** Issue
#583: a freshly bootstrapped 7.6.0 node accepted the opt-in — the
pre-set answer under issue #219 Decision 7 — and stopped immediately at "ask your
operator to set the service address." `netbbs.managed_dns.state.
set_service_url` had no caller anywhere in the installed package: no CLI
flag, no `netbbs.toml` key, no admin screen. The address had no route
into the database every other part of the feature reads it from, so
every node that accepted the offer dead-ended identically, and the
message sent each SysOp looking for an operator who was themselves. The
original reasoning — that the production address is an operational
decision independent of this client code — was right about *where the
decision lives* and silent about *how it travels*; an operational
decision still has to be expressible somewhere. Both halves exist now.
`DEFAULT_SERVICE_URL` (`netbbs.managed_dns.state`) carries the project's
own instance, the same shape and the same reason as `netbbs.link.
reliable_nodes.RELIABLE_NODES_URL` — a project-run service a node must
not need to be told about in order to use. `[managed_dns] service_url`
(or `--managed-dns-service-url`) points a node at a different one, which
is what a self-hoster running their own `services.managed_dns`, or the
project pointing a node at a staging deployment, needs. The configured
value is mirrored into the node database once per startup, *including
its absence*: an operator who removes the setting returns that node to
the shipped address rather than leaving it pinned to an override it was
told about once.

Making the address configurable makes it *changeable*, which the
credential's own design (Decision 2) has to answer: a managed-DNS
credential is a bearer secret for one service's registration, so a node
that already holds one must never present it to a service that did not
issue it. The issuing address is recorded in the same transaction as the
registration it belongs to, and every path that would send the
credential checks it first: the updater pauses its heartbeat (saying so
once in the log), release, rename and cancellation refuse and name the
two addresses that disagree, and registration against a new service
starts over as a fresh registration rather than presenting the old
secret — behind one confirmation, because it replaces the credential
file and leaves the registration at the old service to lapse on its own.
For the same reason the setting is restricted to `https://` unless it
names a loopback address: the service's own process speaks plain HTTP
behind a TLS-terminating proxy, so the node's side of it is the proxy's
address, and a plaintext hop to a remote host would put the credential
on the wire on every heartbeat.

`DEFAULT_SERVICE_URL` is `https://dns.netbbs.org`: the project's
instance on Roanoke, behind the same Apache that serves the website,
against the same BIND that serves the zone (roadmap tracker #612, step
2). From v7.7.0 until that deployment the constant was `None` and a
node simply had no service to register against. The opt-in was still
asked, and still recorded exactly once, per Decision 1 and issue #219
Decision 7; what differed was only that the node said plainly that the
service was not running yet, and it still says so if the constant is
ever reverted. Deliberately not "hide the question until the service
exists": the decision being asked is whether this node may contact
project infrastructure at all, the first-run screen is the one moment
that question is naturally in front of a SysOp, and deferring it would
mean either re-opening a settled consent question later or enrolling a
node that had said yes to something narrower.

**Decision 9 (locked in, implemented) — a SysOp may change their
managed name, and the change is make-before-break: the old name stays
live until the replacement has matured and published, then enters the
ordinary cooldown.** The workflow shipped with its invariants recorded
(the closing paragraphs below) but without the decision above them
(issue #601), so a reader asking "can I change my board's name, and
what happens to the old one?" found the guarantees only by knowing to
look among implementation notes. The decision: `Change [N]ame` on the
DNS screen reserves the replacement as a second, pending registration
linked to the current one (`replaces_name`), holding a second bearer
credential on disk beside the first. The node heartbeats both. While the
replacement matures — the same age gate as any registration — the old
name is canonical and keeps resolving; the status screen shows the
current name and the reserved one, and `[C]ancel change` is offered the
whole time. The heartbeat that first publishes the replacement's record
is the one that releases the old name, in that order (publish, delete,
then commit both rows), so callers never see a gap; the old name then
sits in Decision 5's cooldown, reclaimable only by the credential that
held it. Why a second credential and a transition journal rather than
something simpler: the two rows are two registrations the service
authenticates independently, a crash between "new credential issued"
and "node knows about it" would otherwise strand one of them, and the
journal is what makes the swap replayable in either direction. The
alternative — release, then register the new name — goes dark for the
whole age gate and forfeits the old name's reclaim window, which is why
the SysOp handbook says not to use it as a rename shortcut.

**Decision 10 (locked in, implemented) — every managed name requires
continuous contact from the node to stay alive; `dynamic` selects only
whether the published record follows the node's address; and a name the
service has abandoned is reclaimed by the node itself when it returns.**
Issue #600 found the Goal describing a "registration without the
updater" that the code had never offered: going live requires a day of
uninterrupted contact, staying live requires contact within every
abandonment window, and the node has no setting that stops the pass
while keeping the name. That is the right design, so the Goal was
amended rather than the code: the heartbeat is the liveness signal the
abandonment sweep uses to free squatted names, which is a Decision 3
abuse control, not a dynamic-DNS implementation detail, and a static
registration that never had to check in would be a name nothing could
ever reclaim. What a static board gets instead is that its record is
written once and never rewritten.

The real cost of that rule was a node back from an outage. Take a board
down for a fortnight's holiday: the sweep abandons the name after a
week, the record leaves DNS, and on the node's first heartbeat back the
service answers 401 — at which point the updater used to stop for good.
The name was held for the node for the whole cooldown, reclaimable by
one keystroke, but nothing said so; a SysOp who did not happen to open
the DNS screen lost it when the cooldown purged it. Abandonment is the
service noticing the node was away, not the SysOp choosing to leave, so
the node now performs that keystroke itself: on every pass while its
cached status is `abandoned` (and no rename is outstanding), it sends
`POST /reclaim` with the credential it still holds, and on success
carries straight on into the ordinary heartbeat. The route is the
safety of it: the service performs the reclaim the credential entitles
the node to, or refuses — it never registers afresh on the node's
behalf. A fresh registration mints a new credential, spends a
rate-limit token and, once the cooldown has purged the row, is for a
name that may no longer be this node's; all of that stays the SysOp's
own `[R]egister`. It is a route of its own rather than a flag on
`/register` so that a service older than it answers 404 and the node
fails closed, where an older service ignoring an unknown flag would
have registered afresh from a background task. A refusal is recorded
as a recovery note the DNS screen shows beside the ABANDONED badge, with
the sentence the service gave — and the screen then stops claiming the
name is held. Which refusals are retried follows from what can change
without the SysOp: the service at capacity, unreachable, or without the
route (an upgrade will bring it) are retried next pass; a 409 — the row
purged, held by another credential, or revoked — is *final*, since no
later pass can change what this credential is entitled to, and the
updater sends nothing more for that name until the SysOp's own
`[R]egister`. Three refusals are answers rather than failures: a row that is *already
active* under the same credential is the retry of a reclaim whose
response was lost, and is answered with the row's state so the node
resumes heartbeating; a `released` row is refused with that word, since
release is the SysOp's decision to stop (Decision 5) and a node
restored from a backup taken before it would otherwise undo it on its
first pass — the node adopts the service's word instead; and a
`revoked` row refuses the automatic path exactly as it refuses the
manual one (Decision 4).

*A check-in that does not happen is never silent (issue #640).* Because
the name depends on contact, a node that holds a `pending` or `matured`
name and did not reach the service says so on the DNS screen: "Last
contact: never" rather than no row, the reason the latest attempt
failed, and no "nothing to do". Two independent v7.9.0 nodes registered,
were told the name would go live once the node had stayed in contact,
and never contacted the service again, and every way that can happen was
silent: a credential file the node's account cannot read (written by
`netbbs.admin` run as another account) raised and ended the updater task
for the rest of the node's uptime, and a node whose only route out is an
HTTP proxy failed every check-in into the log. Hence three rules. The first check-in is
sent by the register flow itself, the moment a registration succeeds, so
the SysOp is told of a failure while still looking rather than promised
a wait. Every reason a pass sends nothing for such a name — unreadable
or missing credential, no service address, a credential another service
issued, an opt-in that is not `accepted`, a request that got no usable
answer, a pass that raised — is recorded for that screen, and the task
survives a pass that raises, since what raises there is repaired without
a restart. And the heartbeat's direct connection stays: the service
publishes the address a check-in arrives from, which through a forward
proxy is the proxy's, so `/register` honouring `HTTPS_PROXY` while
`/heartbeat` does not is deliberate — it is stated where the failure is
shown, since a SysOp cannot guess it. A node with no direct route cannot
hold a managed name today; letting one declare a static address instead
is an open question, not something the node works around.

**Implemented.** Node-side client (`src/netbbs/managed_dns/`, shipped
inside the installable `netbbs` package: opt-in prompt, credential
storage, the periodic heartbeat/updater task, the SysOp status/register/
release admin screen) and the managed-service backend
(`services/managed_dns/`, a separate deployable the project itself
operates, never packaged into a node install, the same relationship the
already-live netbbs.org website has to its own separate deployment) —
including a real `Rfc2136DnsProvider` (TSIG-signed RFC 2136 dynamic
updates against a self-hosted BIND server, this project's own
infrastructure choice, not a commercial DNS API) behind the same
`DnsProvider` interface a `LoggingDnsProvider` satisfies for every
automated test. Decision 3's originally-locked review queue was dropped
during implementation planning (see that decision's own closing
paragraph above) in favor of a simpler, symmetric hard-reject shape for
both the rate limit and the cumulative cap. Shipped implementation-time
parameters, all constructor-injectable and reasoned defaults rather than
values this design doc fixes: a 24-hour minimum-age qualifying period
(Decision 3), a 5-per-hour-refilling, 5-capacity service-wide
registration rate limit (Decision 3), a cumulative cap of 1000 active
registrations (Decision 3), a 7-day no-contact abandonment threshold
before a registration is swept as abandoned, and a 90-day cooldown
shared by both voluntary release and abandonment (Decision 5, "on the
order of 90 days" as locked in above). A fourth terminal status, `revoked`, and the
token-gated `/admin/revoke` behind it carry Decision 4's manual dispute
process (issue #599); `MANAGED_DNS_CONTACT` is the channel Decision 3's
refusals name (issue #598). Actually standing the backend up
— a host, DNS delegation, a real BIND server's `allow-update` ACL and
matching TSIG key — is an operational step the code does not perform on
its own; see `services/managed_dns/README.md`. It is done: the project
instance answers at `https://dns.netbbs.org`, and the shipped
`DEFAULT_SERVICE_URL` names it (Decision 8). Turning the zone dynamic
had two consequences the runbook now records: hand edits to the zone
file go through `rndc freeze`/`thaw`, and every static name in the zone
is in the service's blocklist first, because the provider *replaces* a
name's address records rather than adding to them.

The minimum-age period measures uninterrupted successful heartbeat
contact, not wall-clock time since registration: first contact starts
the window and a gap beyond the abandonment threshold resets it. DNS
provider mutations run outside the HTTP event loop and share one bounded
transition lane with their surrounding database mutation. A sweep,
heartbeat, release, or reclaim therefore cannot commit from state made
stale by another provider await; concurrent HTTP transitions receive a
retryable rejection before entering the worker queue. Publication and
deletion failures remain retryable state (release is not finalized until
deletion succeeds, and a failed static publication is retried). Successful
replacement maturation carries that publication result through the rest of the
heartbeat and never issues a second publication from the stale pre-transition
row. The service-wide token-bucket state survives backend restarts.

Node-side updater passes and interactive registration, release, rename, and
cancellation transitions share one process-local transition lock per node
database. The lock covers the remote mutation and its local reconciliation, but
never a human prompt, so a heartbeat response from an older snapshot cannot
overwrite a completed SysOp transition. Node-side rename state and interactive
registration/reclaim results are each one local database transaction: the
replacement name, pending/unpublished state, and previous-name presentation state
never become visible in a partial combination, nor can a reclaimed status appear
with a stale published flag. Heartbeat reconciliation follows the same
rule for the active and previous names, statuses, publication flags, and contact
time. A credential-specific HTTP 401 is authoritative independently of the
other credential's transient result, and all inactive outcomes from one pass
are applied in that single transaction. Cancellation and updater-led recovery
first commit the remotely revived
previous name as a coherent local state, then use the credential transition
journal in reverse to restore the previous bearer secret and remove the
replacement-era extra secret. The service's cancellation response supplies the
revived name's authoritative publication state; a failed republish cannot revive
a stale local publication claim. A crash on either side remains recoverable by
the retained two credentials and the updater. Point-in-time restore likewise
removes any primary, previous, or journal credential artifact absent from the
backup generation. A replacement abandoned during a rename remains eligible
for credential-recovery retry only while its own release/abandonment cooldown
is still open, and reactivation restarts its contact window at the retry time.
Recovery of a replacement which is still marked pending also records fresh
contact and restarts its maturation window only if the prior contact gap crossed
the abandonment threshold. A successful standalone registration or reclaim
clears any expired previous-name presentation state and obsolete previous
credential rather than leaving a phantom cancellable transition.
Rename completion releases or deletes the previous row only when it still belongs
to the same node fingerprint; a cooldown-expired name reissued to another node is
never mutated through the stale replacement link. Cancellation performs the
same ownership check before any provider deletion, including when the reissued
previous row is already active. Rename retry likewise treats an existing
`replaces_name` relationship as recoverable only when the replacement and
currently authenticated registration have the same node fingerprint; reissue
never transfers control of an old replacement credential. If its heartbeat
becomes
authoritatively inactive while the old
name's heartbeat fails transiently, the node continues heartbeating the retained
old credential on later passes rather than letting the still-live name expire.
A successful old-name heartbeat is also reconciled when the replacement
heartbeat fails transiently: the replacement's cached state remains unchanged,
while the old name's authoritative status and publication result are committed
atomically.
An authenticated successful rename also refreshes the current registration's
contact window before reserving or recovering its replacement, so a waiting
abandonment sweep cannot immediately withdraw the promised old name. An inactive
rename target whose cooldown has elapsed is deleted and admitted as a fresh
replacement immediately, matching ordinary registration rather than waiting for
the periodic sweep. Successful cancellation refreshes the retained
previous registration's last-contact time even when it was still active. An
uninterrupted pending name preserves its earned maturation window, while one whose
last contact crossed the abandonment threshold restarts that window just like a
heartbeat.

### Issue #219 — Reliable Link as default onboarding infrastructure

**Goal:** turn Reliable Link (the project's own persistent, internet-
reachable node, run on hosting with roughly a decade-plus operating
history) into the first entry of a small, resilient "reliable nodes"
list new installs can use to get online frictionlessly — as a default
Link seed, an async relay candidate, and (once #168 ships) a live-chat
relay anchor — without becoming a hard dependency or a single point of
failure or responsibility. Builds directly on #168 (real-time relay
protocol) and #201 (managed DNS) without duplicating either's own
decisions; this entry is the product/architecture layer that sits above
both.

**Terminology (locked in):** official prose always says "Reliable
Link" — the same "spell it out in anything official" convention already
established for "NetBBS Link" itself (never "the Link" outside casual
shorthand). "ReLink.NetBBS.org" stays the DNS-only shorthand — a
hostname, not the node's name in any document or UI copy.

**Decision 1 (locked in) — purely a configurable default, never
protocol-privileged.** Nothing in the Link protocol may treat any
reliable node's fingerprint as special, required, or protocol-blessed.
Technically indistinguishable from a SysOp typing in any other seed —
this is the concrete answer to "does this violate the no-central-master
design," not just an assertion of it.

**Decision 2 (locked in) — a list, not a single hardcoded node.**
Resilience by actually having more than one reliable node from day one,
not resilience-in-theory because the field happens to be configurable.
Reliable Link is the flagship/first entry. If it were ever to stop
operating, the intended failure mode is "some other already-established
reliable node is already in the list," not "someone has to
specifically step up" at that moment.

**Decision 3 (locked in) — discovery is hybrid.** A hardcoded fallback
list ships in source (always works, even offline or if the live
endpoint is briefly unreachable on a SysOp's very first run) plus a
live-fetched list from a stable project-controlled endpoint (under
`netbbs.org`, which the project already controls and presumably outlives
any one node), preferred when reachable. Keeps the roster current
without requiring every installed node to upgrade NetBBS itself to
learn about a change.

**Decision 4 (locked in) — one list, three consumers, not three
separate mechanisms.** Default Link seeds for peer discovery; async
relay candidates (falls out of the *existing* `relay_selection.py`
reliability-ranking automatically, once a reliable node is a known peer
with relay serving enabled — no new selection mechanism needed); and
the live-relay anchor for #168's raw-proxy design. This is also the
answer to one of #168's own still-open questions ("does live relay need
its own consent/capacity concept, or reuse the async model's?") — for
v1, neither: just try reachable reliable nodes from the same list.

**Decision 5 (locked in) — relaying carries zero Phase 4 trust
implications.** Using a reliable node as a relay is not that node
vouching for the relayed traffic's origin, and trusting a reliable node
as infrastructure is not a signal about anyone else who also relays
through it. A reachability decision, not a trust decision — matches how
existing async relay consent already works.

**Decision 6 (locked in) — a node cannot participate in Link with an
unset/placeholder display name.** Surfaced during this discussion, not
originally part of it, but adopted alongside it: today's default node
name is the literal placeholder `"NetBBS"`; every default-installed node
sharing that same display name would make human-to-human conversation
about "which node am I talking to" incoherent the moment Link
participation is easy enough that many nodes actually use it. Enforced
before Link participation, not before local-only operation — a
SysOp running a purely local board never has to touch this.

**Decision 7 (locked in) — first-run UX is one screen, two separate,
defaulted-to-accept choices, not one bundled yes/no.** Relay/seed
participation and the public DNS name (#201) are offered together on
one friendly first-run screen, each pre-set to accept (so accepting
both is a two-Enter-keystrokes path) but each independently declinable
with a plain-English explanation of what accepting means. Deliberately
not collapsed into one "get online easily" choice: both declines have a
real, coherent meaning a SysOp might actually want (relay without a
public name — mesh reachability without independent discoverability; or
a public name without relay — already has direct connectivity sorted),
and bundling them would silently reintroduce the exact consent problem
that made #201 land on opt-in in the first place, for whichever half of
that combination a given SysOp didn't actually want.

**Implemented (issue #266), with the previously-open implementation
choices settled as follows.** The live roster is
`https://www.netbbs.org/reliable-nodes.json` — a JSON object
`{"version": 1, "nodes": [{"name", "url"}, ...]}`, served as plain static
content from the project's own web host (source copy and runbook in
`services/reliable_nodes/`), fetched once a day by
`netbbs.link.reliable_nodes.run_scheduled_reliable_nodes_refresh` under the
same off-switch as the release check, bounded at 32 entries with per-field
length caps, and rejected as a whole on any other format version so an old
build keeps its last good copy. A successful fetch *replaces* the built-in
fallback rather than merging with it, so removing a node from the roster
actually stops it being dialed. `[link] enabled` is now tri-state: an
explicit TOML/CLI value always wins; a silent configuration defers to the
node-wide participation decision (`netbbs.link.onboarding`), resolved once
at startup, so accepting on the first-run screen turns Link on as an
outgoing-only node with no port to open. The participation decision also
gates whether the roster is dialed at all, independently of how Link came
to be enabled — a node upgraded in place never dials project
infrastructure until its SysOp says so. Decision 6 is enforced at the
startup boundary: with Link effectively enabled and the placeholder name,
`python -m netbbs` refuses with a `StartupError` naming the fix, the same
shape as the no-SysOp refusal; the first-run screen asks for the name
before recording an accept so a fresh install never reaches that refusal,
and the console's `Settings > Join NetBBS Link` screen refuses to accept
under the placeholder for the same reason. The first-run screen
(`netbbs.net.onboarding_flow`) lives at the two anchors issue #201's prompt
already used — `netbbs.admin`'s first-SysOp bootstrap and, as a fallback,
a SysOp's authenticated login — each choice checking its own state so an
upgraded node is asked only what it never answered. Still open: Reliable
Link's own operational configuration (relay-serving is already on by
default; live-relay capacity planning waits for issue #168).

### Issue #270 — multi-hop live relay and cross-node /private

**Goal:** close the two gaps §8.10.3's first vertical left open --
relay-of-relay live sessions and the private-conversation mode across
nodes -- without reopening the raw-proxy decision (§16 "Issue #168").

**Decision 1 (locked in) — three steps, all shipped together:** anchor
advertisement (`live_relays` in the signed endpoint descriptor),
dial-the-anchor (a requester that can reach the target's advertised relay
meets it there, single hop), and the chained bridge (two relays, A–R1–R2–B)
only when the requester cannot reach any of the target's anchors. Most
cross-anchor cases never chain; chaining is the resilience path, not the
common one.

**Decision 2 (locked in) — discovery is the target's advertised anchors,
not relay-side search.** The requester names the relay to forward toward
(`via_relay`), taken from the target's own descriptor. A relay-side
fan-out ("ask every reliable node whether B is there") was rejected: it
turns one request into up to a roster's worth, adds latency, and needs no
descriptor change only at the cost of search traffic on every miss.

**Decision 3 (locked in) — hop bound of one forward.** A forwarded request
(`hops: 1`) is never forwarded again, so a chain is at most two relays and
every relay's own caps and byte/idle bounds apply per hop. Longer chains
would need routing state no relay currently keeps and are not a v1 need.

**Decision 4 (locked in) — `/private user@node-fingerprint` ships; cross-
node `/dm` invites do not.** Private mode rides the existing live
direct-message path line by line (small); a cross-node fullscreen direct
chat needs mutual invite/accept frames and a shared room spanning two
nodes -- a separate step, roughly the size of the direct-message vertical.

All frame additions (`via_relay`, `hops`, `for_fingerprint`) ride real-time
protocol v3, unreleased at the time, so no further bump was needed.
Normative description: §8.10.3.

### Issue #611 — password change and reset — closed

Until this issue shipped, `users.password_hash` was written once, at account
creation, and had no update path anywhere: no Profile field, no user-detail action, no admin
CLI command. A forgotten password meant delete-and-recreate, which loses the
account's history and, per #594, frees its Link identity for the next
registrant. Normative description: §4.1.

**Decision 1 — one domain function, three surfaces.** `set_password` is the
only writer, in the same transactional shape as the other account setters;
the Profile field, the user-detail `[P]assword` action and the CLI subcommand
all go through it, and the two screens share one implementation
(`netbbs.net.password_screen`), the same shape the SSH-key screen already
has. Who may call it is the caller's decision; what must hold regardless
(no blank password, no clearing without a key) is the function's.

**Decision 2 — the proof depends on who acts, not on the target's level.**
An account acting on itself proves the current password first; a SysOp
acting on another account does not, since they cannot know it, and the audit
row names them. A SysOp may reset another SysOp's password, matching what the
key screen already allows a SysOp to do to any account; the usable-SysOp
invariant (§4.3) is unaffected because a reset never removes a way in. The
local CLI never asks for a current password: filesystem access to the
database is its trust boundary, as for the rest of that tool, and its
purpose is the locked-out SysOp.

**Decision 3 — the in-session proof is throttled by the login throttle.**
`Session.login_throttle` is set once at login and the current-password
prompt charges it before verifying, in the same order as the login prompt.
Rejected: a separate per-session counter, which would have been a second
budget with its own limits to explain.

**Decision 4 — a key-only account sets its first password without proof.**
The session that reached the screen authenticated by key. Rejected: refusing
until a SysOp intervenes, which would make the only self-service route out of
key-only depend on someone else.

**Decision 5 — masked entry is a documented exception to §3.5, not a draft
editor.** Current password, new password, confirmation: three masked
prompts, each cancelling on a blank line, nothing written before the last.
Rejected: a draft editor, which would have to hold the plaintext password in
the draft across redraws so that `[S]ave` had something to save, and whose
"inspect before saving" value is nil for a value the caller cannot see. §3.5
and `AGENTS.md` both list the exception.

**Decision 6 — a caller's own choice meets the registration floor.**
Self-service applies `MIN_REGISTRATION_PASSWORD_LENGTH`, as both
registration prompts do for a password a remote caller picks; otherwise
Profile would be a way around the floor one screen after registration. A
SysOp setting someone's password keeps the latitude the create-user screen
already gives them.

**Decision 7 — Argon2 stays off the database lane.** The foreground
`DatabaseLane` has one worker; a hash or verification there stalls every
other interactive database operation for its duration and bypasses the
bounded password worker login uses. Both screens and the CLI therefore hash
and verify through that worker (`hash_password_off_loop`,
`verify_password_off_loop`) and run only the short transaction
(`set_password_hash`, `load_password_hash`) on the lane. The synchronous
`set_password`/`password_matches` remain for tests and for callers that own
their thread.

**Declined — guarding this setter against SQLite rowid reuse.** A target
deleted and another account created while the screen waits for input could
inherit the id and receive the update. Every setter in `netbbs.auth.users`
re-fetches by id the same way, and the case needs the highest-id account
deleted and re-created under an open SysOp screen, which is past the
single-operator boundary this project calibrates against. The identity-reuse
question is issue #594's, and its answer applies to all the setters at once.

**Not done, deliberately.** A password change does not end the account's
other live sessions; a caller who suspects a compromise asks the SysOp to
disable the account, which does. No self-service recovery exists: there is no
email or other out-of-band channel to send anything through, so "forgot my
password" is a SysOp action, and the Profile help text says so.

### Issue #596 — who may pull an attestation, and what opting out retracts — closed

Found by the review of the v7.7.0 release. Two facets of one gap between what
a caller is told when they share a SysOp-verified birthdate or real name over
Link and what the node does.

*Disclosure scope.* `_handle_attestation_pull` admits any completed peer whose
signed request is fresh and whom `LinkPolicyAction.TRUST` allows, and
`load_issued_attestation_page` takes no requester at all. The receiver's
`link_attestation_authorities` is a list the issuer never sees. So every
federated peer can read every attestation this node has signed, while §5.5
will not show the same value to the node's own SysOp.

*Retraction.* `_revoke_issued` stamps the row and leaves `envelope_json`, which
carries `attested_value`, in place; the page query filters on nothing. A peer
that links next year and pulls from the start reads a value whose subject
opted out this year.

v7.7.0 shipped consent text that says both things honestly and enforces
neither. These decisions replace that text with enforcement. Normative
description: §5.5.

**Decision 1 — an issuer-side recipient list, and it starts empty.** A new
SysOp-configured, audited set of node fingerprints: the nodes this node tells.
The pull handler keeps every gate it has and adds one: the requester must be
on the list. An empty list means nothing leaves the node whatever its callers
have toggled. An upgrade seeds the list from nothing. In particular not from
the attestation authorities: whose verifications a SysOp accepts says nothing
about whom they are willing to tell, and the two lists point in opposite
directions. Rejected: correcting the consent text and enforcing nothing, which
is what v7.7.0 did as a stopgap and which leaves a value §5.5 withholds from
the local SysOp readable by every peer.

**Decision 2 — a recipient is a node, not a node and an attribute.** The
receiving side scopes an authority to `age` and `name` separately, and the
mirror image was considered. Rejected for two reasons. The caller already
scopes per attribute, with two separate toggles, so the finer knob exists
where the consent is. And a per-attribute grant makes each requester's stream
a function of its grant history, which the protocol cannot express: the pull
cursor is the subscriber's, it names a position, and a requester whose scope
widens has already moved past the rows it was not entitled to last week.
With a per-node list that cannot happen, because of Decision 3.

**Decision 3 — a node that is not a recipient is refused outright, and told
so.** HTTP 403 with `reason_code` `not_an_attestation_recipient`, in the shape
a policy rejection already has. Not a revocations-only stream, although a
revocation carries no value: serving part of the stream advances the
requester's cursor past the attestations it was not shown, and a later grant
would then deliver none of them until each came up for renewal. A refused
requester's cursor does not move, so a grant, a removal and a re-grant all
resume from the right place with no cleverness on either side. The refusal is
visible rather than silent because what it discloses is a relationship between
two nodes that the other SysOp has to act on, by asking to be added. §12.8's
silence protects a per-user gate decision; this is not one.

A node removed from the list stops receiving revocations too. That cost was
accepted here and is reversed by issue #632 Decision 2: removal now sends the
node an empty snapshot, which makes it forget what it held.

**Decision 4 — the caller is told how many, the SysOp sees which.** The
consent text says the value reaches only the nodes the SysOp has named, that
the SysOp may name more later, and the toggle shows how many that currently
is, so a caller can see that sharing to nobody shares nothing. Rejected:
per-caller approval of each recipient. A caller cannot evaluate a node
fingerprint, and re-asking every sharing caller at every grant would make the
list unusable by the one person who can evaluate it. The SysOp is already the
party the caller trusted with the verified value itself; choosing where the
node's assertion of it goes is the same trust, not a new one.

**Decision 5 — a retired attestation stops being served and loses its
value.** When an issued attestation is revoked or expires, the issuer blanks
the columns that carry the value (`attested_value`, the envelope, the
signature) and keeps the row: content ID, attribute, timestamps, what revoked
it. That removes the value from the live, served database; it is not forensic
erasure, and "Not done" below says what it leaves. The
page query serves only live attestations and every revocation, and filters on
liveness at read time so that what is served never depends on when a sweep
last ran. The migration redacts rows already revoked. This supersedes two
sentences of §5.5: "neither expiry nor revocation deletes the signed
historical row" and "the served stream includes expired and revoked objects".

Resumability survives, which was the stated reason for serving history whole.
A cursor names a position, and the tombstone keeps that position resolvable
for a subscriber whose last object was the one redacted. What a returning
subscriber needs from history is the revocation of anything it holds, and
every revocation is still in the stream, after the object it retires. A
retired attestation the subscriber never received is nothing it needs:
acceptance already ignores it, and serving it would hand over a value whose
subject has withdrawn it. A revocation whose target the subscriber never
received is skipped per object, as it is today.

**Decision 6 — a receiver forgets what it is told is withdrawn.** On ingesting
a revocation, and once an attestation it holds has expired, a node blanks the
stored value and envelope and keeps the row, which its revocation, effective
projection and audit rows reference. This is what "switching it off withdraws
it" can honestly mean between two nodes running this software, and the consent
text says the rest: a node that already copied the value cannot be forced to
forget it.

**Not done, deliberately.** No per-caller recipient choice and no
per-attribute recipient scope, for the reasons above. No revocation delivery
to a removed recipient. No way for an issuer to learn what a recipient did
with a value. No secure erasure: redaction blanks columns in the live
database, so the write-ahead log until its next checkpoint, freed pages, and
any backup taken while a value was shared can still hold it, and the issuing
node's own verification record keeps the value for as long as the SysOp keeps
the verification. The threat answered is a peer pulling a value, not someone
reading the disk; turning on SQLite's `secure_delete` for the whole node to
narrow that further was not judged worth its write cost.

**On upgrade.** Attestation sharing stops until the SysOp names recipients.
That is the intended default, and the release notes have to say it, because a
node that was sharing will otherwise read as broken.

### Issue #594 — a deleted account's username stays retired on a Link node — closed

`local_user_id` is the username, and `users.username` is unique only among
live rows, so deleting an account frees its Link identity for the next
registrant. Link mail sends and delivers by it (`deliver_link_message` resolves
the recipient with `get_user_by_username`), a carried post's `author_label` is
`username@home-node-fingerprint` and `link_events` keeps it, the trust subject
is derived from it, and a remote identity attestation names it. A
re-registered name inherits all of that: authorship, trust state, a live
attestation until its revocation propagates, and mail a remote sender wrote to
the previous holder. Normative description: §4.3.

**Decision 1 — retire the name; do not change the identifier.** A stable,
never-reissued per-account identifier was the alternative. It needs a
migration assigning one to every account, a wire decision about the
`node_vouched_user` payload every peer already has on disk, a display
decision because an opaque ID is exactly what must not be shown, and a
compatibility story for labels peers retain. It also would not be enough: a
Link mail address is `name@node` by design, so whoever holds the name gets
the mail whatever identifier sits underneath. Retiring the name is the part
every answer needs, so it is the part built.

**Decision 2 — recorded at deletion, on a node that has ever run Link.**
`delete_user` writes the retired name in the transaction that deletes the
account, when the node runs Link now or has at any time before. "Ever" is a
sticky marker the node sets the first time it starts with Link effectively
on. The migration that introduces it seeds it on a node that ran Link before
the marker existed, from any artifact Link leaves behind: a stored peer, a
retained event, a linked board, channel or file area, a piece of Link mail,
or a recorded decision to run Link. Stored peers alone would not do, since a
node can originate a linked board and republish it later without ever having
stored one. A node that has never run Link records nothing and
its names stay reusable, which is both BBS tradition and the cost the issue
names: a small board should not lose names forever to a network it is not on.
A recorded name stays retired whatever the Link setting does afterwards.
Registration checks the record case-insensitively, as the uniqueness index
does.

One kind of account is not retired: a registration still awaiting approval
that was never sent Link mail. Declining registrations is routine on an
approval-required node, and retiring those names would let strangers
permanently consume names merely by asking for them. The exemption rests on
the pending state because that state proves the account never had a session:
it is set only at registration, cleared only by approval, and refused by
every login path. "Never logged in" was considered and rejected as the test,
because it proves nothing: open registration hands a new caller straight into
a first session without recording a login, so an account that registered,
posted on a linked board and never returned has no login on record. Received
Link mail is part of the test because it reaches an account by name without
the account doing anything: delivery resolves the recipient by username,
pending approval or not, and acknowledges it. An approved account that was
never used is retired like any other; the SysOp can release it. The screen that confirms a deletion and the
deletion itself ask one shared predicate, so the warning cannot promise a hold
that does not happen.

The test is "ever", not "now", because what peers hold does not evaporate
when Link is switched off. Keying on the setting at the moment of deletion
would let an account that was Link-visible be deleted during a maintenance
window with Link off, re-registered, and then carried back onto the network
under an identity its peers already know. The price is over-reservation on a
node that once ran Link and no longer does, where an account that never
federated is retired anyway; the SysOp can release it (Decision 3), and the
alternative is tracking per account whether it was ever Link-visible, which
every Link subsystem would have to feed.

**Decision 3 — the SysOp can release a name, on purpose.** A list of retired
names reachable from user administration, with release as a confirmed,
audited action. A SysOp who deleted a test account, or whose caller comes
back after asking to be removed, is making the trust decision this project
leaves with the SysOp. Without a release the only repair is editing the
database.

**Decision 4 — a remote caller is told the name is unavailable; a SysOp is
told why.** Self-service registration refuses a retired name in the words it
uses for a taken one, so registration is no oracle for who used to have an
account. The create-user screen and the admin CLI say that the name belonged
to a deleted account, that it is held because the node is on Link, and where
to release it.

**Decision 5 — forward only.** The migration creates an empty table. The
moderation log names past deletions, but in free text, and reconstructing
reservations from a log line is the kind of inference that retires the wrong
name. Names already freed stay freed.

**Not done, deliberately.** Nothing repairs an identity a peer already holds
for a name reused before this ships. Nothing in the codebase renames an
account; a rename, if one is ever built, frees a name the same way a deletion
does and has to retire it the same way.

### Issue #589 — automatic equivocation signals — decided

The open half of the entry below: when a node accuses another. Of everything
the code observes, only equivocation -- one node signing two different objects
into the same slot of one chain -- carries proof a receiver checks itself
(#1036). Revoked-key use, invalid signatures, bad authority, floods and
malformed traffic are observer claims nobody else can reconstruct, and no
production code records them as measurements. Normative description: §12.5,
§12.6, §12.9; the Handbook's "Policy trust settings".

**Decision 1 — fully automatic, equivocation only** (the maintainer's choice;
the alternatives were the node proposing and the SysOp signing, or no
issuance at all). Where a chain refuses an extension because its slot is
taken -- a key-transition chain, a post's content chain, a board's lifecycle
chain -- and both objects verify under one node's keys, the node keeps both,
records a self-verifying local observation, which quarantines that node's
identity integrity here (§12.8), and signs a `signed_equivocation` signal with
both objects embedded. Reordering (an extension of a head not on file yet) and
an exact resend are not forks.

**Decision 2 — receivers verify, and two domains still have to agree.** The
signal counts at a subscriber only once its proof reproduces there (#1036),
and only toward the two-domain threshold, so one node's automatic accusation
does not by itself quarantine its subject anywhere else.

**Decision 3 — bounded, switchable, revocable.** At most one live signal per
subject and five signed in any 24 hours; never about the issuing node itself.
A node-wide switch (Settings → Policy trust → Signals, on by default) stops
issuing and revokes every live signal on the next pass. A SysOp can revoke one
signal and keep the evidence, and clearing the evidence revokes its signal.
Honest forks happen -- a node restored from an old backup, a cloned VM -- so
the Handbook says what such a signal about one's own node means.

**Decision 4 — recovery waits for a SysOp** (§12.9), as the section already
required; until this, every trigger recovered after the 24-hour hold.

**Decision 5 — nothing left unwired.** The digest-evidence path went with
#1036 (its Decision 4). `build_trust_signal`, `build_equivocation_evidence`,
`record_local_observation` and `clear_local_observation` now have callers, so
#589 leaves the production-callers allowlist.

### Issue #589 — trust-object issuance — slice 1 decided and built; signals open

Until this slice no node could issue a trust object. `trust_wire` verified,
stored, re-served and enforced on a peer's signed objects, and nothing in
`src/` called a builder, so the carrier store was empty on every node and
§12.7's pull served nothing. No dogfood run, however long, could have shown
trust propagating. Normative description: §12.4 and §12.7.

**Decision 1 — slice 1 is the vouch, and only by a SysOp's act.** A vouch is
the one trust object whose trigger needs no judgment about evidence: a person
decides to stand behind an identity. When a node *accuses* another, which
observations become a signed signal, and whether any of that is automatic,
stays open in the issue. `build_trust_signal`, the digest-evidence path and
the local-observation lifecycle remain without a production caller, and the
ratchet in `tests/test_link_production_callers.py` still lists them.

**Decision 2 — an intent, and one reconcile that signs.** The screen records
an intent; `reconcile_issued_vouches` decides what should exist and signs it.
This is the shape remote attestation already has, for its reasons. The offline
admin console has no node identity, so a screen that signed would not work
there. And two code paths that each decide when a signed object should exist
eventually disagree, so the sync pass and the SysOp console call the same
function, and the listing derives its status from the same predicate.
Rejected: signing in the screen, with the sync pass only renewing.

**Decision 3 — own objects live in the carrier store.** A vouch this node
signs is stored in `link_trust_wire_objects` under its own fingerprint, which
is exactly what the existing pull serves for a requested issuer. No new
endpoint, no new wire type, no separate issued-objects table. It is not
recorded in `link_trust_vouches`: that is what local policy counts, and it
counts only configured reporters.

**Decision 4 — the reason is mandatory, bounded and published.** It is inside
the signed payload, so every subscriber stores it, and the screen says so
before asking for it. Replacing it retires the old vouch and signs a new one,
since signed bytes cannot be edited. 280 characters, because nothing on the
receiving side bounds it short of the response's byte limit.

**Decision 5 — 90 days issued, renewed at 30, reissued on key rotation.** The
180-day ceiling is what a receiver tolerates; a node that goes dark cannot
withdraw, so the issued lifetime is the window in which an unseen withdrawal
still leaves support standing. The same numbers as an issued attestation, so
there is one pair to remember. Rotation is detected by verifying the stored
signature under the current key rather than by remembering which key signed,
which needs no column and cannot drift.

**Decision 6 — a restriction here suspends the vouch, and the intent
survives it.** Vouching for an identity the issuing node has quarantined or
blocked says something the node does not act on, so the reconcile revokes it
and the screen refuses to record one. §12.4 makes that safe: a revocation
accuses nobody. The intent is kept so that lifting the restriction restores
the vouch without anyone having to remember it.

A rotation also re-signs what it would otherwise orphan. A subscriber that has
learned the new key can verify nothing the old one signed. For a vouch the
reissue covers that. A revocation is reissued by nothing, so one signed shortly
before a rotation and not yet pulled would be skipped as an old-key object,
and the vouch it retires would stay live at that subscriber until it ran out.
The reconcile therefore signs such a revocation again under the current key
while its target has not expired; a subscriber that already holds the first
one skips the second as a repeat.

**Decision 7 — the receiving side stops treating an unusable object as a
hostile response.** Three rejections that were fatal to a whole batch became
per-object skips, and changing a reporter's grant now resets its cursor
(§12.7). None of this could be reached while nothing was ever issued. With one
real issuer and one subscriber that has established it, it is reached as soon
as the issuer's SysOp vouches for a caller and the subscriber only granted it
node vouches: the batch is abandoned and that subscription never moves again.
The same would go for the first operational-key rotation. A slice that let
nodes issue vouches and left their subscribers wedged would not have made the
subsystem real.

The rotation handling above became reachable with issue #624, which built the
rotation itself; the Phase 4 exercise gained its rotation row there.

The same decision covers a responder that no longer knows a subscriber's
cursor, filed as #621 while this slice was in review and fixed with it for
both pulls, because it is the same wedge by another route and the recovery
exercise restores a node on purpose.

An object signed by a key the subscriber does not know is the one case that is
*not* skipped, because there the subscriber may be the stale party, and a
skip would lose every object the issuer re-signed. The first cut of this slice
skipped it; review caught that it turned a rotation into permanent loss.

**Decision 8 — pages in storage order.** See §12.7. The attestation page made
the same change for the same reason.

**Not done, deliberately.** No vouch for the node's own users and none for
itself. No trust signals. A subscriber told nothing about *why* an object was
skipped beyond its diagnostics log. (Carrier pulls in the sync loop, also
left out of this slice, followed in issue #627.)

### Issue #630 — content from a node this one has never met — closed

Found by running the planned three-node deployment in the loop harness: one
full node and two outgoing-only nodes that seed off it and share one of its
boards. Normative description: §8.11.

A node could verify nothing signed by a node it had not completed a hello
with, and two nodes complete one only when one dials the other. §8.8 accepted
that as "rarely restrictive" on the expectation that small deployments end up
fully meshed. They do not: a node dials its seeds, everyone is given the same
seed, and two outgoing-only nodes cannot dial each other. So in the ordinary
topology the two never saw each other's posts. Under enforced policy the
unknown author read as probationary and was refused quietly, was not listed
as a trust subject, and so could not be established; each refusal was
downloaded again every pass, and 200 of them starved the subscription. With
the author established by some other route, the stranger check rejected the
carrier's whole response from the first post.

**Decision 1 — a carrier may serve a third node's hello bundle.** §10.6 and
the #90 entry had already worked out why this is safe and deferred it as not
needed. It is needed. The bundle verifies against itself, so the carrier is
trusted with nothing and can at worst withhold.

**Decision 2 — introduced is not met.** An introduced identity verifies what
it signed and does nothing else. It cannot push, pull, relay or be mailed,
and it is stored apart from peers so that no present or future check of "is
this a peer" can mistake it for one. Rejected: a flag on the peer record,
which would have made every reader of that table responsible for remembering
it, the mail path among them.

**Decision 3 — introduce before policy, and start on probation.** The
introduction grants nothing, and it is what makes the node visible to the
SysOp who alone can establish it. Rejected: introducing only authors policy
already admits, which is circular, since a node that is not a subject is
never admitted.

**Decision 4 — carriers pass on introduced identities too.** Rejected: the
peer list's first-hand-only rule. That rule exists because a secondhand
address degrades with distance; a bundle does not.

**Decision 5 — one unusable event costs one event.** Responses from a carrier
are handled per event, and an event that cannot be used yet is set aside and
declared as seen. Rejected: only skipping it, which leaves it to be sent
again every pass and lets a page fill with such events. A refusal that does end
a response is returned with what was accepted before it, which the caller
persists: a batch that raised half-way had already put its earlier events
into memory, the caller persisted none of them, and being now "known" they
were never accepted, and so never persisted, again.

**Decision 6 — a stale bundle is refreshed from the carrier.** Key
transitions of third nodes are not gossiped, and this slice does not change
that; it asks again instead.

**Decision 7 — a met node outranks an introduced one by name.** Introduction
puts names on file that nobody here vouched for by dialing. The
familiar-name comparison is therefore one-sided (§8.11), mail addresses
resolve among met nodes only, and meeting a node re-judges every introduced
node that wears its name, whichever was on file first. Rejected: first-seen
wins throughout, which hands a real peer's name to whoever posts first.
Between two nodes neither of which this node has met it is still the rule,
there being nothing to tell them apart.

**Decision 8 — a carrier answers only for whose content it served.**
Rejected: gating the route like the peer list, which a peer on probation is
refused. A new node is on probation at its seed for its first month, so that
would have withheld introductions from exactly the nodes that need them.

**Not done, deliberately.** Verifying §5.5 attestations against an introduced
identity; trust objects followed in issue #627. Link mail to
an introduced node (§10.6 stays deferred). Automatic graduation for nodes
never met. Any way for an introduced node to be dialled, which includes
fetching its files. Persistence of the set-aside list, which is rebuilt at
the cost of one repeat per event after a restart. A way for a requester to
tell a responder which signers to leave out, which is what would lift the
10,000-event bound.

### Issue #627 — what a node nobody can dial issues — trust objects closed, attestations open

Found while working out how the Phase 4 exercise could run on the three live
nodes, two of which are outgoing-only. Trust objects and attestations are both
pulled from their issuer, and an outgoing-only node has no address, so a vouch
its SysOp issued was signed, stored and served to nobody, and said nothing
about that. Normative description: §12.7.

**Decision 1 — the issuer deposits at its relays; subscribers pull the
carrier form.** The wire already defined a pull whose responder is not the
issuer, and the cursor was already kept per responder and issuer. Rejected:
flooding trust objects with content, which §12.7 rules out for the reason it
always did, and having subscribers wait to be dialed, which an outgoing-only
subscriber never is.

**Decision 2 — deposited objects are stored apart from admitted ones.**
Carriage and authority are different facts about an object, as "introduced"
and "met" are about a node (#630). Rejected: a flag in the admitted store,
where an object present counts as replayed and would never be applied after a
later reporter grant.

**Decision 2a — a relay admits what it carries in its own pass, never on
arrival.** Rejected: admitting inside the deposit handler, which only ever
sees one request's objects. A depositor named a reporter afterwards, a
widened grant and a batch rejected for a reason time undoes would each have
left objects that are never offered again.

**Decision 2b — a deposit names the object it continues from.** A depositor
sends only what is new, so nothing else would ever tell it that a relay has
lost something. Rejected: depositing everything on every pass, and trusting
the relay's count.

**Decision 3 — relay consent is the gate.** A relay already decides whom it
holds mail for and how many; the same decision covers trust objects. Rejected:
accepting deposits from any established peer, which would make every full
node a store for every node it knows.

**Decision 4 — only the issuer deposits.** The objects authenticate
themselves; their order does not.

**Decision 5 — verification accepts an introduced issuer; authentication of a
wire peer does not.** `resolve_known_signing_key` is for checking something a
third party delivered. Every route that authenticates its caller keeps
`resolve_peer_signing_key`.

**Decision 6 — attestations take their own path.** Decided in issue #632's
entry: a sealed snapshot per recipient, through the recipient's relays.

**Not done, deliberately.** A node that drops a relay tells nobody, it only
stops naming it, so a relay does not forget what it carried for it; expiry and
the per-depositor bound are what limit that, and revocations, which do not
expire, count against the bound. For the same reason a relay decides whether
to read its own copy by the depositor's descriptor and not by its own record
of whom it agreed to relay for. A
subscriber does not try relays it has not met. SysOp-written trust signals
remain #589's next slice.

### Issue #639 — what expiry means to a caller — closed

Found when issue #638 made the file area keystroke-only and took
`/download <filename>` with it, which was the last path to a file that had
expired. The contract of `list_files_page` promised expired files were
"delisted from normal browsing though still individually reachable", and
`get_file_by_name` kept returning them, so the promise survived its only
surface. Normative description: §5.3.

Two things found while deciding it, which changed the shape of the answer:

- **`posts.py` makes the same promise and never had a surface either.**
  `get_post`'s contract says "expired content stays individually reachable,
  only delisted from normal browsing", and `netbbs.net.board_flow` contains
  no reference to an expired post and no call to `get_post`. So this was not
  a regression #638 introduced in file areas; it was one sentence written in
  two subsystems, of which files happened to hold an accidental instance.
- **`get_file_by_name` had no production caller left.** Every remaining
  mention was a docstring in the past tense. The promise was being kept by a
  function nothing called.

**Decision 1 — expiry ends a caller's reach, in both subsystems.** A caller
may rely on this: expired means gone. Rejected: a `show expired` toggle on
the file listing, and returning expired rows from `[/] Find` labelled. Both put
delisted content back in front of callers, which is the one thing expiry
exists to stop, and the second costs the most to build — the expiry sweep
calls `reindex_file`, which *deletes* a row from `file_search` as soon as it
stops being `approved`, so search would need a status dimension carried
through every consumer in order to re-expose what it had just removed.

**Decision 2 — the domain keeps its unfiltered lookups, for two named
consumers.** Reply-parent resolution needs an expired thread to resolve, and
SysOp recovery needs an expired file's row to reach its bytes. What changes is
the contracts, which stop describing either as something a caller can do.
Rejected: filtering expired rows out of the domain, which would break replies
to a thread that expired mid-conversation.

**Decision 3 — the SysOp recovery surface is a file-area screen, not a
caller-facing one.** Expired files are listed on the file area's admin detail
screen with `[D]ownload`, mirroring the pending-file review screen #638
already gave that action to, and offered under the same transport rule
(issue #475).

**Decision 5 — `get_file_by_name` is deleted.** Decision 3 was first expected
to give it a production caller again. Building the screen showed it does not:
the recovery screen is a picker, which hands over the row the SysOp chose, and
#638 had already moved `send_file_to_caller` off re-reading a file by name
because a filename is not unique within an area. Routing recovery through the
by-name lookup would have handed back the oldest row of that name, possibly an
approved file rather than the expired one picked. With no caller the function
went, rather than stay as another implemented, tested and unreachable name. Its
pending rule lives on where it is enforced: `list_pending_files` and the
transfer path.

**Decision 6 — a transfer link does not outlive expiry.** A download link
minted while a file was listed is refused once the file expires, its uploader
included, the same way an outstanding link does not outlive a lockout. It
serves an expired file only to a holder of `APPROVE` on the area, which is who
the recovery screen mints it for.

**Decision 4 — posts get no equivalent screen.** A file is an artifact that
is unrecoverable once the grace period ends; a post is text in a board. The
recovery case that justifies decision 3 does not arise, and inventing a
screen for symmetry would be building for nobody.

**Not done, deliberately.** `test_expired_file_still_reachable_by_name` stays,
renamed `test_expired_file_keeps_its_row_until_purged` and pinned through
`get_file` now that the by-name lookup is gone, since the domain behaviour it
guards is still true and still load-bearing: what was wrong was the promise
about callers, not the return value. The recovery screen has no restore
action; a re-upload is how a SysOp puts a file back. The §3.5 bullet recording #638's trade also
cited §11 for the delisting rule, which §11 does not state; it now points at
§5.3, which does.

### Issue #624 — guided operational-key rotation — closed

§4.5 said rotation was "a guided SysOp action", but no screen, command or
task rotated an operational key. The receiving half was built and tested and
could not be reached. Checking what a rotation would actually do found a
second gap. Every event branch verified a signature against the sender's
*current* key only, which made §4.5's "historical signatures remain
verifiable" false. So the first signing-key rotation on a real node would
have left all of its boards, posts, files and in-flight mail unverifiable to
any peer that had not received them yet, carriers' copies included, and a
board it originated could no longer be joined.

**Decision 1 — two kinds of rotation, stated by the root.** A rotation either
*retires* the old key or declares it *compromised*. The distinction is an
optional `"compromised": true` on the root-signed `revoke`. A retirement is
byte-identical to every revoke built before the field existed, and older
receivers ignore the field. Since no node had ever rotated, the meaning of a
plain revoke was still free to choose. It means retirement, because the
common case must not cost anything. Rejected: one mode in which every
rotation is a compromise, which would make routine hygiene re-sign and
re-push the node's whole history. Also rejected: honouring every key the
chain ever authorized, which would make rotation no answer to a leak, the
very case the issue exists for.

**Decision 2 — which checks accept a retired key.** Long-lived events accept
one: genesis, posts and their revisions, lifecycle events, channel messages,
file descriptors, mail and its acknowledgements. They are checked against the
current key and then every key retired without being called compromised
(`verifying_operational_keys`). Everything signed fresh for one exchange
still checks the current key alone: hellos, descriptors, requests, chunk
descriptors and withdrawals. So do trust objects and attestations, which
their issuer re-issues on rotation (§12; #622 for vouches, #623 for
attestation revocations). There an old-key signature is a stale copy or a
replay, never history.

**Decision 3 — a compromise response re-signs the node's own objects under
the same ids.** A content id does not cover the signature, so a re-signed
object is the same object to every peer. `resign_own_content` selects rows by
the signature itself: an object is re-signed exactly when it verifies under
one of the node's compromised keys. It runs after the rotation and again at
every startup, where it costs nothing on a node with no compromised key and
finishes a response that a stop interrupted.

A copy that another node already holds is not reached. It stays accepted
there, and a node that later pulls it from that carrier skips it *per
object*, as it does a trust object signed by a superseded key: a stale copy
must never end the response it arrives in. A carrier that has learned the compromise holds such a copy as
stale and asks for it again, so the re-signed copy reaches it and then the
nodes behind it (§8.11 "Stale copies", issue #672).

**Decision 4 — a rotation is saved before anything live changes.** It is also
journaled, because the node is running. The new key is staged beside its
file, `transitions.json` is replaced (the commit point), and only then is the
key moved into place. `NodeIdentity.load` finishes a staged key the chain
already names and discards one it does not. A save that fails leaves the
running node exactly as it was. Retired signing keys are kept under
`retired/` for one purpose: opening mail a peer sealed to the old key before
it learned the new one. Nothing signs with them.

**Decision 5 — the running node reads its identity at the moment of use.**
`LinkContext.node_identity` and `LiveDirectChat` read through
`LinkNode.identity` instead of holding a copy. A caller who opened a screen
before the rotation would otherwise go on signing with the revoked key. The
real-time listener and every standing anchor connector hold their own copy,
so a transport rotation hands them the new identity *before* it closes the
sessions keyed to the old one. Otherwise a reconnect could race ahead with the
key being retired.

**Decision 6 — the surfaces.** On the running node the action is **Link
status → Keys**. The screen shows the root fingerprint, which rotation never
changes, and each key's history. **Signing key** and **Transport key** each
open a screen with **Rotate** and **Compromised**, and each of those takes
one confirmation. On a stopped node the action is `python -m netbbs.admin
rotate-key {signing,transport} [--compromised] [--identity-dir DIR]`, which
refuses while a node process holds the database. Both surfaces write a
`rotate_node_key` audit entry. The Phase 4 exercise gained a rotation row.

Not built: a screen action that declares an already-retired key compromised.
The chain and `operational_key_history` accept that second revoke, and
nothing issues it yet.

### Issue #628 — real-time Link through an HTTP `CONNECT` proxy

Decided during the roadmap re-evaluation of 2026-09-18 (issue #612): a network
that permits outbound traffic only through an HTTP proxy is one of the settings
Link is meant for, and there asynchronous Link works (every outbound
`aiohttp.ClientSession` sets `trust_env=True`) while live chat does not.
Normative description: §8.10.

What the code does today, which set the shape of the answer:

- **Three places open a real-time socket, and they all end the same way.**
  `dial_realtime_session` (direct dials of a peer, a relay, an anchor or a
  linked channel's origin), `attach_relayed_session` (a party joining a
  relayed bridge, which writes the plaintext `NETBBS-BRIDGE/1` attach record
  before Noise starts) and `RealtimeRelay._handle_upstream_ready` (a relay
  forwarding to an upstream relay, issue #270, which never runs Noise at all).
  Each calls `asyncio.open_connection(host, port)` on an address taken from the
  peer's signed descriptor and then works on the stream pair. Nothing below
  that line knows or cares where the bytes go, so a tunnelled stream
  substitutes cleanly — including for the attach preamble, which is just more
  bytes written ahead of the handshake.
- **An outgoing-only node dials directly as well as through relays.** No
  real-time dial checks `outgoing_only`: a DM tries the target's advertised
  real-time addresses first, a linked channel dials its origin, and every
  participating node keeps anchor connections to the reliable nodes. So the
  gap is not the relay leg alone; on a proxy-only network every real-time
  path fails.
- **The advertised port is already independent of the bound one.**
  `realtime_advertised_port` exists and is what the signed descriptor carries,
  so a node can bind 8862 and advertise 443 behind a forwarder today.

**Decision 1 — one helper opens every real-time socket, and it tunnels when a
proxy applies.** All three sites call a single function in place of
`asyncio.open_connection`. When the environment names a proxy for the target,
the helper connects to the proxy, sends `CONNECT host:port HTTP/1.1` with a
`Host: host:port` header, requires a `2xx` answer, and returns the same stream
pair; Noise, the attach preamble and the relay pipe then run over it unchanged.
The target comes from a peer's descriptor, which is signed but whose addresses
nothing validates today, so the helper accepts only a strict authority — a DNS
hostname, an IPv4 literal or a bracketed IPv6 literal, and a port in range —
and refuses anything else before a byte reaches the proxy; otherwise a peer
could inject request lines into the operator's authenticated proxy. The target
host is sent to the proxy unresolved, because on a proxy-only network the local
resolver commonly cannot resolve outside names. Of the Link traffic, the proxy
sees what any on-path observer of a direct connection sees: the dialled address
and, on a relayed attach, the plaintext `NETBBS-BRIDGE/1` record with its
single-use attach token, which travels in the clear on a direct connection too.
Everything after it is Noise, authenticated end to end (§8.10). What a direct
connection does not have is the proxy's own credentials: with Basic
authentication (decision 4) they cross the `http://` leg to the proxy in the
clear, exactly as they already do for the asynchronous half's proxied requests. The descriptor-pinning rules for relay
attach addresses are unaffected because they compare the address before the
socket is opened. The upstream-relay site
is included although a relay is by definition reachable: the rule "every
real-time socket is opened here" is what keeps a fourth site from quietly
reopening the gap. Rejected: a proxy-aware variant of each caller, which is
three implementations of one handshake.

**Decision 2 — the proxy comes from the environment only, and it is the one
asynchronous Link uses.** A peer's Link endpoint is advertised as `http`, so
`aiohttp` sends that peer's boards and mail through `HTTP_PROXY`; the tunnel
uses the same variable, falling back to `HTTPS_PROXY` when only that is set, and
`NO_PROXY` exempts targets, all read with the standard library's own rules
rather than a parser of ours. Using the proxy that already carries this node's
asynchronous traffic is what keeps the two halves from disagreeing when the
variables differ. Only `http://` proxy URLs are supported. A SOCKS or `https://`
proxy URL that applies to a target fails that dial with a visible configuration
error and is never treated as absent: a configured proxy is the only way out,
and falling back to a direct dial would bypass the operator's egress policy
wherever direct traffic happens to be possible. Rejected: a `[link] proxy`
setting. It would be a second source of truth beside the one the
async half of Link already reads, and the two halves of one node could then
disagree about how that node reaches the network.

**Decision 3 — when a proxy applies, the tunnel is the only attempt.** There is
no direct dial first and no fallback to direct afterwards. A network that needs
a proxy usually drops rather than refuses direct outbound connections, so a
direct-first rule would make every live dial wait out its full timeout, once
per advertised address, before the path that works. This is also what `aiohttp`
does for the async half: with a proxy configured it uses it. A SysOp whose
proxy should not carry some peers says so with `NO_PROXY`, which is the
standard lever for exactly that. Rejected: tunnel-on-failure, for the latency
above and because it makes the path a dial takes depend on timing rather than
on configuration.

**Decision 4 — proxy authentication is Basic, from the same sources
asynchronous Link reads, and nothing else.** Credentials in the proxy URL's
userinfo, or failing that the `~/.netrc` entry for the proxy host (which
`aiohttp` consults under `trust_env`), become a `Proxy-Authorization: Basic`
header on the `CONNECT`. NTLM, Negotiate and Kerberos are out of scope: the
async half of Link does not speak them either (`aiohttp` does not), so building
them for live chat alone would leave a node with live chat and no boards. The
supported answer for such a network is a local authenticating shim that
presents an unauthenticated proxy on loopback, which serves both halves at
once; the operator documentation names that pattern.

**Decision 5 — no NetBBS change for the port; it is an operator recipe.**
Corporate proxies commonly allow `CONNECT` only to 443 (Squid's stock
`deny CONNECT !SSL_ports`). A reliable node that wants proxy-only callers to
reach it live advertises its real-time address on 443 with
`realtime_advertised_port` and forwards that port to its real-time listener.
NetBBS does not bind 443 itself — the same rule as the web transport (issue
#201) — and does not multiplex Noise with TLS on one port; a host whose 443 is
already taken by an HTTPS front end needs a second address, or a protocol
demultiplexer in front of both, and either is the operator's choice. Rejected for now: a
second advertised real-time address per node. The descriptor already carries a
list and dialers already try each entry in order, so adding one later is a
compatible change, but nothing needs it until a reliable node cannot move its
only real-time port.

**Decision 6 — the tunnel is bounded, and its failures are recorded where they
happen.** One timeout of the helper's own covers connecting to the proxy and
reading its answer, and the answer has a header-size ceiling, since
`dial_realtime_session` itself has no overall timeout and the anchor connector
calls it without one; a proxy that blackholes the connection, or accepts it and
then says nothing, must not hold a dial open. On every path that does not
return the stream — timeout, an oversized or malformed answer, a refusal,
cancellation — the helper closes the proxy connection and waits for it to close
before raising, since no caller holds the writer yet; anchor retries would
otherwise leak a socket per attempt. A refusal
(`403`, `407`, `502`, anything not `2xx`) raises a distinct transport error
naming the status. The helper also records the last tunnel outcome on a
node-owned status object, and a dial whose tunnel opened but whose handshake
then failed records that too — "tunnel opened, handshake failed" is its own
outcome and never shows as a success, because the anchor connector swallows every dial
exception and a caller's `DirectChatUnreachable` deliberately carries no
reason; the Link status screen shows the proxy in use and that last outcome
("tunnel refused: 407 Proxy Authentication Required"), and a change of outcome
is logged at WARNING once rather than on every retry. Callers keep the generic
unreachable message: a caller cannot fix a proxy and the SysOp now can see it.

**Decision 7 — a TLS-inspecting proxy is a known limit.** A proxy that
terminates or inspects TLS inside the tunnel (Squid `ssl_bump`, most
"SSL inspection" appliances) will reject a stream that is not TLS, and Noise is
not TLS. That shows up as a tunnel that opens and then fails its handshake, which is
the outcome decision 6 records. Wrapping real-time Link in TLS to pass such a proxy is
not part of this decision; it would be a separate one, taken only if a real
deployment meets it.

**Testing.** A real loopback `CONNECT` proxy in the suite (an asyncio server
that parses the request, rejects one without `Host`, optionally demands Basic
credentials, and pipes bytes), per the rule about real boundaries: a direct dial, a relayed attach
with its preamble, and an upstream-relay leg each through it; `NO_PROXY`
bypass; a `407` surfacing as the recorded outcome; a silent proxy and a
blackholed one both bounded by the timeout; an unsupported proxy URL failing
the dial rather than dialling direct; a descriptor address with CR/LF refused
before the proxy sees it; the proxy observing EOF after each failed setup. A ratchet test that no `asyncio.open_connection` remains in
`netbbs.link` outside the helper. The live check is the proxy-only node joining
the dogfood deployment and holding an anchor session to a reliable node.

**Not done, deliberately.** Managed DNS stays as it is: its heartbeat connects
direct on purpose, because the service publishes the address a check-in
arrives from and a proxy-only node has no address worth publishing (issue
#201). This
decision does not change that asymmetry, and the helper is not used there.
Implementation is its own issue.

### Issue #561 — the Link carry model: what the cap decides, and what "declined" means

Carry is opt-out: joining Link is the only decision a SysOp makes, and each
verified genesis becomes a local board, channel or file area on arrival, up to
`max_carried_boards` / `max_carried_channels` / `max_carried_file_areas`
(500 each). The issue asked whether the property that falls out of that —
*which* resources a node carries is decided by sync arrival order, and the
rest are declined forever — was chosen or accidental. It was accidental.
Normative description: §9.3.

What the code does today, which set the shape of the answer:

- **"Declined" is never recorded; it is inferred.** A genesis in `link_events`
  with no local row reads as "seen and declined" (`inventory_wanted_ids`), and
  three different histories produce that state: a quota refusal, a SysOp
  deleting a carried resource (the delete functions leave `link_events`
  alone), and a crash between `save_event` and `materialize_carried_board`,
  which are separate lane calls with no repair path. Nothing can tell them
  apart, so §9.3's promise of a visible local exclusion "represented honestly
  as 'not carried on this node'" had no implementation behind it.
- **Arrival order decides, and nothing revisits it.** Responders walk
  resources in sorted-id order; the requester dials configured seeds before the
  reliable roster. The 501st genesis of a type is refused and, being inferred
  as declined, never asked about again.
- **The declined state also starves sync.** A resource this node does not carry
  is absent from its `InventoryRequest`, so every responder carrying it sends
  its genesis and every event under it on every pass, under the one 200-event
  budget shared by all three types. Recorded as issue #669, with a
  reproduction.
- **The cap is exempt for what already landed**, so lowering it sheds nothing,
  and only boards have a readout (`Carried boards: N/500`); channels and file
  areas have none.

**Decision 1 — carry stays opt-out.** A node that joins Link carries what
arrives, and scenario 1's "small node joins a busy Link" keeps working without a
SysOp curating anything. What changes is everything past that default.

**Decision 2 — a genesis this node has accepted is in exactly one recorded
state: carried, offered or excluded.** *Carried* has a local row. *Offered* and
*excluded* are rows in a carry-decision record keyed by resource kind and id,
holding the state, when it was set and by whom (the node, or a SysOp). Nothing
is inferred from absence any more. Every transition is one transaction: the genesis save
with its carry outcome (materialization or the record), and each later move
between states — accepting, excluding, deleting, restoring — with the local row
and the record changing together. That needs non-committing variants of today's
`materialize_carried_*` and `delete_*` helpers, which commit internally. It
closes the genesis crash window the way issue #73 closed it for posts, rather
than adding a repair pass for it. On migration, every existing genesis without a
local row becomes *offered*: its history cannot be recovered, and offered is
the state that loses nothing and forces nothing.

**Decision 3 — past the cap a new resource is offered, not declined.** The
cap bounds what the node takes on automatically; it no longer decides what the
node can ever have. An offered resource is listed for the SysOp with its name,
description and origin node, and `[A]ccept` materializes it from the stored
genesis, replays any other events this node already holds for it (a migrated
resource, or lifecycle events accepted beside a genesis past the cap, would
otherwise be declared as known and never projected), and then the next pass
backfills the rest by ordinary inventory pull. Accepting is the SysOp's choice and is not
itself capped. A genesis whose name is already taken locally is carried under a
disambiguated local name, on automatic intake and on accept alike, and the SysOp
can rename it as any carried resource; names are not identities in Link.
Today such a genesis aborts one sync pass and is then never carried (issue
#671). While offered, a resource wants nothing further, for the reason
§8.8 gives for declined ones: asking about content this node has not taken on
does not terminate. Rejected: reordering automatic intake to prefer reliable
nodes, configured seeds or busier resources. Reliable nodes are never
protocol-privileged (issue #219, decision 1), and any order is still an order;
once nothing is lost past the cap, which resources arrive first decides only
which ones need no click.

**Decision 4 — the cap governs intake, not holdings, and a cap of 0 is a
curated node.** Lowering a cap sheds nothing; a SysOp prunes by excluding, which
is a choice about named resources rather than a second arrival-order rule run
in reverse. A cap of 0 offers everything new and carries nothing unasked, which
is scenario 2's "these ten, nothing else" without a separate mode. A slot freed
by an exclusion is not filled from the offered list: once a resource has been
put in front of the SysOp, accepting it is theirs to do, and an offer that can
silently turn into a carried resource later makes the list untrustworthy.

**Decision 5 — deleting a carried resource hides it; Restore un-hides it; Purge
deletes it (amended 2026-09-26, the maintainer's decision).** Deleting a carried
resource keeps its local row and everything in it and sets `link_hidden_at`:
callers and content administration no longer see it, it is not carried or
declared as carried, no new content is projected into it, and it is recorded as
excluded (`deleted`) -- all in one transaction with its audit entry. An
`Excluded` list under Link status shows it with `[R]estore`, which clears the
mark so the resource is back exactly as it was, and the next sync pass pulls
what arrived meanwhile; and `[P]urge`, behind a typed-name confirmation, which
deletes it for real and leaves it excluded (`purged`). After a purge, and for an
offer declined without ever being carried, `[R]estore` takes the resource on
again from its stored genesis, like an accepted offer. A hidden resource's name
stays taken, and creating or renaming onto it says which excluded resource
holds it.

The first form of this decision deleted the rows and restored by replaying
`link_events`, keeping this node's own users' envelopes and a local-moderation
overlay so the replay could be faithful. Mapping the code showed it could not
be: this node's own posts are keyed by a local content hash rather than their
event's content ID, so replayed they come back detached from their local
accounts; and a carrying node's moderation is not in the signed history at all.
Hiding keeps both exactly, costs one column, and leaves reclaiming the space to
an explicit Purge.

Exclusion applies only to a resource whose *current* origin, resolved as §9.4
resolves it, is another node — not merely one whose genesis came from
elsewhere, since a board transferred to this node makes it the authority peers
depend on for closure, moderator edits and further transfer. Removing a linked
resource this node is the current origin of is not a carry choice and is outside
this decision: for boards it goes through closure or origin transfer (§9.5, §9.4);
linked channels and file areas have no transfer or closure events yet, so their
delete path stays as it is today, a gap this decision records rather than
closes -- deleting such a resource stays a real delete. The SysOp can also
exclude an offered resource without ever carrying it. This answers scenario 5 (it stays gone, and
visibly so) and scenario 3 (pruning frees slots for automatic intake; nothing
that was offered is lost). Excluded and offered resources are never visible to
callers.

**Decision 6 — what a node does not carry, it tells its peers.** Offered and
excluded resources are declared in every `InventoryRequest` so responders stop
sending events under them, which is the fix for #669 and the #630 precedent
(events set aside under probation are declared as seen). The existing maps cannot
say it: their values are known-ID sets, and a responder sends every event whose
ID is absent, so declaring an offered resource's genesis alone still brings
every post. The fix therefore needs an explicit "not carried here" field,
signed, and sent only to a responder that has said it understands it: an older
responder rebuilds the signature payload without an unknown field and would
reject the whole request, and bumping the protocol version would stop hello
from completing, since versions must match exactly. So the responder advertises
an inventory capability — in its hello or descriptor — and the requester sends
the field only where it is advertised. Against an older responder there is no
suppression: nothing it understands can express it, and declaring received IDs
the #630 way would need a retained-ID store for events this node never keeps,
since posts, messages and descriptors under a resource without a local row are
dropped without reaching `link_events`. Mixed versions keep today's behaviour
until the responder upgrades. The declaration costs one resource ID per offered or
excluded resource; the carried maps beside it list every content ID of every
carried resource and are far larger, so keeping the whole request under the
responder's size limit is one problem for both. Issue #685 settled it for both
at once by paging the declaration (§8.8), rather than a separate bound on the
carry-decision record.

**Decision 7 — the bound stays a per-type count, and all three get a
readout.** 500 stays the default, and the declared scale (§2.3) stays small.
Link status shows `carried/cap` for boards, channels and file areas alike, and
the offered and excluded counts beside them. Rejected for now: a storage or
activity bound. Two of the three types are already bounded where their cost
concentrates — `max_remote_files_per_area` for a file area's catalogue and the
node-wide scrollback retention limit for a channel's history — and boards are
not: a carried board's posts are durable, unbounded-lifetime state (§8.9), and
no cap shape changes that, since a board grows after it is accepted. A storage
bound would need the node-wide disk accounting §13.9 lists as its own future
slice; until then a count is the number a SysOp can reason about when choosing
what to accept, and excluding a board that grew too large is the lever.

**Decision 8 — the vocabulary is the one Link Communities will use.** §6.5 says
carrying a Community carries its present and future members with per-resource
and whole-Community exclusions. In these terms a Community subscription is a
standing rule that accepts offers naming that Community; opting one member out
is excluding it; and a resource in no Community stays under the node-wide
automatic intake, so unfiled content is never uncarryable by construction.
Leaving a Community does not shed members already carried, for the reason in
decision 4. Whether a subscription has a bound of its own, and how a subscribed
Community is told apart from a same-named local one, stay Phase 6 questions;
nothing here forecloses them.

**Not done, deliberately.** Offers add a row per genesis this node already
stores, so they grow nothing a peer could not already make it store; bounding
genesis intake as a whole is a separate question, and this decision does not
change it. Bulk actions on the offered list (exclude everything from one
origin) wait until a list long enough to need them exists. Implementation is its
own issue, after #669.

### Issue #511 — HTTP transfer hardening — closed

Split out of PR #508, whose fixes did not converge. Normative description: §6.2.

**Decision 1 — two ceilings on an HTTP upload.** `max_upload_bytes` keeps
meaning file bytes, as over Zmodem; the request gets that plus a fixed framing
allowance. Rejected: one ceiling on the whole body, which makes a browser's own
framing refuse files within the configured maximum and, with a small enough
setting, makes browser upload impossible.

**Decision 2 — `HEAD` tells the truth.** It answers as a `GET` would, without
spending the grant. It used to answer 204 for every token so as not to be an
oracle for guessing them; with 256-bit tokens an oracle offers a guesser
nothing, and the page needs the answer. Rejected: a separate check route, which
exposes the same information on a second path.

**Decision 3 — probe, then stream.** The page probes with `HEAD` and saves
through `<a download>` only on a yes. Rejected: `fetch` plus `blob()`, which
buffers the whole file in the page; a CORS header on the endpoint, which widens
who may read a transfer response; and the `public_url` link as given, which
fails cross-origin. The page resolves the token against its own address
instead, which is same-origin and keeps a reverse-proxy prefix.

**Not done, deliberately.** The probe is not a reservation: a file deleted, or a
slot taken, between probe and `GET` still saves that `GET`'s error body. The
window is one round trip.

### Issue #711 — an art post's layout on the Link

A post drawn in the ANSI art editor keeps its lines: each line stays a line, and
only a line wider than the terminal wraps (§6.1). Which layout a post has is set
by the editor that wrote it, not by an author-managed flag. For a carried post
to keep its lines everywhere, and not only on its origin, the layout has to
travel with it. Normative description: §6.1 and §9.2.

**Decision 1 — an optional `layout` field on `board_post`.** `"layout": "art"`
for an art post. The key is omitted for prose, never `null`, per §7.2's omission
rule. No `netbbs_protocol` bump: §7.5 allows optional fields that old peers can
safely preserve, and they do. A node that predates the field verifies the
signature over the bytes as received, keeps the envelope and relays it
unchanged, and shows the post as prose, which is the intended fallback. A value
this node does not know is prose too, so a later layout degrades the same way.
Rejected:
- a marker inside the body, which every older node would display;
- a new event type, which older nodes would store opaquely and never show at
  all (§7.5).

**Decision 2 — the root carries it; edits follow.** An edit is written in the
editor that drew the post, so `board_post_edit` and the moderator edit carry no
layout: a revision takes its root's. The local `posts.layout` column is
meaningful on the root row. Rejected: a layout per revision, which would let one
edit turn a drawing into reflowed prose with nothing to say why.

### Issue #675 — an author's withdrawal on the Link

An author may withdraw their post: its text becomes "[withdrawn by author]"
(§6.1). An ordinary author edit to that text would reach every node, but a
node that carries the board with its own moderation, or that holds this
author's posts for trust review, holds an incoming author edit for approval.
There the withdrawn text would stay up until that node's moderator acted.
Normative description: §6.1.

**Decision 1 — an optional `withdrawn` field on `board_post_edit`.**
`"withdrawn": true` on the author's edit; omitted otherwise, never `false`,
per §7.2's omission rule. It is signed by the author's home node like any
author edit, so it needs no new authorization. No `netbbs_protocol` bump, for
the same reasons as issue #711's `layout`: §7.5 allows optional fields that old
peers can safely preserve, and they do. A node that predates the field
verifies and relays the edit unchanged and shows it as an ordinary edit, held
where it holds edits. That is the old behaviour, so nothing gets worse there.
Rejected:
- a new event type, which older nodes would store opaquely and never show
  (§7.5);
- reusing `board_post_tombstone`, which is origin-signed and terminal; a
  withdrawal is neither.

**Decision 2 — honored only for a withdrawal and nothing else.** A carrying
node lets the edit through without local moderation or a trust hold only when
it takes the text away and changes nothing else: the body is exactly the
placeholder and the subject is the predecessor's. Otherwise the flag would
carry new text past every node's moderators. Anything else carrying the flag
is an ordinary edit, moderated as one. A local tombstone still ends the chain,
and a post nobody here approved stays unpublished either way.

**Decision 3 — it clears the pin and the keep.** On every node, locally
(`posts.withdrawn`, `trg_posts_withdrawal_clears_flags`), so a withdrawn post
neither stays at the top of a board nor outlives its expiry.

### Issue #767 — the node map — decided

A caller had no way to see which boards the network holds, and a SysOp could
see only the nodes this one had met. Normative description: §8.12, and §8.2 for
`dial_in`.

**Decision 1 — callers and the SysOp, one screen.** The SysOp's view is the
same list with more rows and columns, and replaces the peer list behind Link
status. Rejected: a SysOp-only screen, which leaves callers the one audience
that needs a BBS list most; and two screens, which would drift apart.

**Decision 2 — guests through the level, not a toggle.** Guest login is an
account and no code branches on it (§4.6), so the map has a minimum level like
everything else a SysOp gates. Rejected: a guest switch on the map, which is
the first thing §4.6 rules out.

**Decision 3 — unverified candidates are the SysOp's alone.** A peer-list
candidate has completed no hello and names nobody who vouched for it, and
anyone can put a name on one. Introduced nodes are listed for callers: they
carry a verified bundle and name their carrier.

**Decision 4 — no opt-out.** A board missing from the list must mean callers
cannot reach it through this board. With an opt-out it could also mean its
SysOp asked, and a caller could no longer tell whether a missing board is
unreachable or merely unlisted; a list that says it may be incomplete is one
nobody trusts.
The one gap that remains is structural, that nodes pass on only nodes they have
met, and the title states it. Rejected: an `unlisted` descriptor flag. Leaving
quarantined and blocked nodes off the caller view is not an opt-out: it is
this board's own judgement, and such a node is unreachable through this board,
which is exactly what its absence tells a caller.

**Decision 5 — "last heard" is first-hand or signed by the node.** Contact
this node observed, or a descriptor time the node signed itself. Rejected: the
peer record's update time, which a peer list refreshes secondhand and which
made dead peers look live (issue #766); and the time a carrier's bundle
arrived, which says when this node asked, not when the other one ran. The
signed time is capped at when this node first stored it, because a skewed
or dishonest clock could otherwise date a descriptor years ahead and never
go stale.

**Decision 6 — dial-in addresses are signed by the board they describe, and
stated by its SysOp.** They travel in the descriptor because a descriptor
reaches every node that knows the board, introduced ones included, and a
carrier cannot alter it. They are stated rather than derived because a node
cannot see what is in front of its listeners (the issue #201 entry, Decision
6); the SysOp's screen suggests entries from the node's DNS name and
listener ports, and publishes nothing the SysOp has not saved, except an
`https://` `[web] public_url`, which the SysOp has already stated. Rejected: deriving them from
the listener ports, which on the shipped defaults would advertise ports that
nobody can dial from outside; and a new wire message, when an optional
descriptor field is how #669 already extended the descriptor. Plain `http://`
is not accepted, for the reason the #201 entry gives for the web listener.

**Decision 7 — a bad dial-in claim costs only itself.** Malformed entries are
dropped where they are read and never fail a hello. Rejected: refusing the
hello, as invalid profile claims are, which would let a typo in a display field
cut a node off from Link.

**Not done, deliberately.** A drawn topology: nodes do not record who passed
on a candidate, and edges beyond this node's own would be guesses. Jumping from
a node's detail view into its boards. Removing stale nodes by age.
Reachability claims beyond direct and introduced, such as whether Link mail
reaches a node.

### Issue #761 — the SysOp's live session monitor, snoop and break-in chat — decided

A SysOp had no live view of what callers were doing and no way to act on one
session beyond a message or a disconnect. The work is split across #762
(activity and idle), #763 (the Monitor), #764 (screen copy and snoop) and #765
(break-in chat). The decisions below are the maintainer's, recorded on the
tracker on 2026-09-27.

**Decision 1: an in-BBS screen, not a local command.** The monitor is
Operations → Node and sessions → Monitor, inside the SysOp console. All live
session state lives in the node process, and `python -m netbbs.admin` reaches
the node only through the database. Rejected: a local `netbbs top`. It would
need a new IPC channel and wire protocol, with no portable form (asyncio can't
serve Unix sockets on Windows), and it would add attack surface. A SysOp who
wants it locally connects to `localhost`.

**Decision 2: the Monitor shows places, never content.** A caller's activity
is a trail of place names, such as `Boards › Retro` or `Doors › Voidrunner`.
It never includes a message subject, a mail or direct-chat partner, a file
name or a search string. Idle time counts caller input only; transport
keepalive does not count.

**Decision 3: snoop is silent, and callers are told in advance.** A SysOp may
watch any live session, the caller's screen exactly as they see it, private
messages included, without the caller being told at the time. What a caller
may rely on:
- **Disclosure.** The last line of chat's `/help` says it: "The SysOp can
  watch any live session." The User Handbook's "What the SysOp can see" says
  it in full.
- **Record.** Every snoop is logged to the node log with the SysOp, the
  caller and the duration.
- **Passwords.** Masked input is never echoed, so it never reaches the screen
  copy that snoop shows.

Rejected:
- an on-screen "being watched" indicator, the classic BBS alternative, which
  the maintainer declined;
- no snoop at all. The per-session screen copy is needed for break-in either
  way.

**Decision 4: break-in chat puts the caller back exactly where they were.**
The SysOp takes over the caller's terminal for a two-pane chat. When it ends,
the caller's screen is repainted as their program left it, including anything
it printed meanwhile and a half-typed line, and they carry on. The mechanism
suspends the session's I/O and never its task (worklog, "Break-in suspends a
session's I/O, never its task").
- A break-in is refused during a binary transfer, and a transfer waits for a
  break-in to end.
- A break-in is refused while the caller is typing a password. A password
  prompt reached during a chat shows `*` there.
- A caller in a door keeps playing unattended during the chat, so the SysOp is
  warned first.
- Only the SysOp ends the chat. Every chat is logged.

Rejected: an "invite to direct chat" stand-in, which the maintainer judged not
enough; and cancelling the caller's task back to the main menu, which loses
their place.

**Decision 5: narrow terminals drop columns, never wrap.** Below 80 columns
the Monitor drops the address, terminal size and transport columns, in that
order. User, idle time and activity always stay. How narrow the console must
still work is decided in §3.4 (issue #662).

### Issue #836 — delegation short of SysOp — decided

A SysOp who stepped away left signups and held posts that nobody could act
on, and the only way to hand them over was level 255, which includes power
over the SysOp who gave it. Normative description: §5.6, with §4.3 and §5.2.

**Decision 1 — named staff permissions, with Co-SysOp as a preset.** Approve
accounts, manage accounts and moderate everything, granted per account, and
a one-step Co-SysOp preset that sets all three. Rejected: a Co-SysOp level
band such as 250-254, the classic BBS convention, because nodes already use
those levels as resource gates, and an upgrade would silently give console
powers to accounts a SysOp raised only to open a board; and account authority
as a new kind of moderator grant, which would stretch a per-resource table
into node-wide authority and still leave no one-step way to hand over the
node's routine work.

**Decision 2 — staff never reach the SysOp.** Staff act only on accounts below
255 that hold no staff permission, cannot raise anyone to 255, and cannot
grant anything. A second SysOp at 255 keeps full power, including over the first;
that is what 255 means, and staff is how to give less.

**Decision 3 — everyday account work, but no deleting and no 255.** Manage
accounts covers what a helper needs while the SysOp is away: disabling and
enabling, password resets for callers locked out, and levels up to 254 so a
helper can open level-gated boards to members. Deletion is permanent and, on a
Link node, retires the name (§4.3), where a disable can be undone by the SysOp
on their return. Level 255 is the SysOp's own authority and is given only by a
SysOp.

**Decision 4 — read and write grants pass level gates.** The bits already
existed and did nothing. Giving them meaning lets a SysOp open an
announcements board to a helper without a new concept. Age and verified-name
gates still hold, because they are facts about the person, not trust the SysOp
extends.

**Decision 5 — away is per person, and never outlives what it says.** A
node-wide notice would be wrong the moment one of two SysOps came back. A
return date that passes ends the notice. A notice without one is shown with
the day it was set and reminded to its owner at each login, so callers can
judge a stale one and its owner is prompted to end it. Rejected: requiring a
return date, which a SysOp who does not know when they will be back could
only guess.

### Issue #843 — aliases that pass for the SysOp; invitations nobody saw — decided

The persona test found `/nick InkWell[sysop]` accepted and shown as
`<~InkWell[sysop]~>`, beside a SysOp whose own status bar reads
"InkWell[sysop]". A direct-chat invitation to a caller in Who's online sat
unseen while the inviter waited. Normative description: §6.3.

**Decision 1 — the username beside every alias.** Refusing look-alikes
narrows the gap; showing `alias|username` closes it, since no alias can then
stand alone. Issue #64 kept the stream to the alias alone as less cluttered.
The field test is the evidence that clutter was the lesser cost.

**Decision 2 — every username is protected from aliases, only SysOp names
from display names.** An alias is chosen to be a different name, so refusing
one that reads as another caller costs nothing. A display name is meant to be
a person's own, and two callers named Anna must both be able to use it.

**Decision 3 — local aliases only.** A Link name is shown as `name@node` and an
MRC name with its board, and neither is granted here, so this node has nothing
to refuse. Rejected: comparing aliases against remote names too, which would
make a local alias depend on who happens to be linked.

**Decision 4 — tell a busy invitee, do not interrupt their screen.** The
notice uses the path a SysOp's message already takes into any screen. Rejected:
answering the invitation from inside every picker, which would spread the
invite handshake across screens that own their own keys; and writing into a
door or a Zmodem transfer, which would corrupt what that screen is drawing or
sending.

### Issue #899 — the speaker label on a chat line — decided

A chat line from a linked node read `<Phase4Ops@OutBound · outbound.netbbs.org>`:
fine once, a lot of width on every line of a conversation, and one run of one
color that did not show where the user ended and the node began. Normative
description: §4.4 and §6.3.

**Decision 1 — the friendly name alone, qualified only when shared.** The DNS
name stays on the screens that have room for it. On a chat line the friendly
name is the only thing marking where a caller comes from, which #843 Decision 3
relies on, so a name another known node or this BBS also uses keeps its
qualifier, in the form the node map already used. The existing caution for an
undismissed cryptographic-identity observation stays in front of the line.
"The same name" includes one that reads the same (issue #900).

**Decision 2 — `Name · abc123` replaces `Name abc123`.** The node map's form
without a DNS name could not be typed back; with the reserved `·` it can, and it
matches the DNS-qualified form. Rejected: a fingerprint prefix alone, which
drops the name a reader knows the node by.

**Decision 3 — style the parts, not the whole.** Brackets and `@` muted, the user
in the speaker color, the node in `NODE_COLOR`. The same split fixes the
alias label: it read `Quill|lena_h` with the username at the alias's weight,
so readers could not tell which was the alias and the alias no longer read as
the chosen name. The alias now leads in its color with the username muted.

**Decision 4 — `alias (username)`, not `alias|username`.** Color alone did not
settle it: a pipe has no settled meaning, and a reader could not tell whether
it joined two names, separated them, or which side was the account.
"Name (handle)" already reads as a name and the account behind it, needs no
fallback on a terminal without UTF-8, and keeps #843's rule that no alias
stands alone. Aliases may no longer contain parentheses, so none can carry a
forged second account, and one set before that is no longer shown: with the
account's own marker inside it, the real username beside it no longer keeps it
honest. The chat status bar, which showed `username (alias)`, now reads
`alias (username)` like the stream, and `/names` marks MRC callers `(on MRC)`
as `/who` does. The verified-name unit keeps its own `(=...=)`
marker. Rejected: `aka`, English and longer on every line; an arrow, which has
no settled meaning and could read as "talking to".

Rejected for Decision 3:
restricting node friendly names to the alias character set, which would make
this node refuse the hello of any existing peer whose name uses one, and would
undo #807's quoting of names containing `@`.

### Issue #900 — look-alike node names — decided

Friendly names were compared by Unicode form and case only, so "0utBound" beside
a known "OutBound" raised no warning and needed no qualifier. With #899 dropping
the DNS name from chat lines, that left the friendly name as the only anchor in
exactly the place where passing for a familiar node pays. Normative description:
§4.4.

**Decision 1 — two keys for two questions.** "Could a reader confuse these?"
uses the presentation skeleton (`look_alike_key`): the cryptographic-identity
warning, and every screen that qualifies a shared name (chat, node map, Who,
the board-origin picker). "Is this the same name?" keeps the exact key: a rename
under one fingerprint, the claim history's deduplication, and resolving a typed
reference. Rejected: resolving typed references by skeleton, which would let a
look-alike node receive what a caller addressed to the real one.

**Decision 2 — DNS names are not folded.** Registration already makes them
unique, and `presentation_skeleton` drops `-` and `.`, so `out-bound.example.org`
and `outbound.example.org` would become one. Screens that show a DNS name show
all of it, so Who and the board-origin picker add a technical identity only
where full labels could still be confused: the friendly names read alike and
the DNS names do not tell the nodes apart, because one has none or both share
it.

**Decision 3 — this node's own look-alike name is a warning, not a refusal.** A
SysOp who renames their node to read like a node it knows is told so and the
name is kept: two hobbyists choosing one name is harmless, and the nodes that
know both already flag whichever arrived second.

### Issue #929 — CP437 terminals: a character set per session — decided

NetBBS sent UTF-8 to everyone, with a Unicode or ASCII choice of decoration.
Classic BBS terminals such as SyncTERM read bytes as CP437, so they showed every
non-ASCII character as two or three characters of noise and never saw ANSI art
as drawn. Normative description: §3.2, "Character set per session".

**Decision 1 — map at `Session.write`, with a curated table.** Every text write
already goes through `Session.write`, so one mapping covers every screen,
caller-written text, preset names and copy with `—` or `…` alike. The table
gives NetBBS's own glyphs deliberate CP437 substitutes. Rejected: a glyph set
passed to every renderer in place of the Unicode-style flag. That flag reaches
975 call sites in 29 files, and even then caller text and ordinary copy would
still carry characters CP437 lacks, so it would need the central mapping as a
safety net anyway.

**Decision 2 — the preference is Auto, Unicode, CP437 or ASCII.** Auto is the
default, and an explicit choice beats detection, so one caller can use SyncTERM
at home and PuTTY at work. The old style preference migrates: off becomes
ASCII, on becomes Auto. Rejected: keeping a separate style flag beside the
character set, which would allow meaningless pairs such as ASCII with Unicode
decoration.

**Decision 3 — an undetected Telnet caller gets ASCII before sign-in, including
the SysOp's banner.** CP437 would garble a UTF-8 terminal and UTF-8 garbles a
CP437 one; ASCII is readable on both. After login the "which line looks right?"
question settles it. Rejected: guessing UTF-8 (the pre-#929 behaviour, which
gave SyncTERM callers noise on their first screen) and guessing CP437.

**Decision 4 — ASCII is true 7-bit.** Before this, turning Unicode off only
swapped decorative glyphs; em dashes, ellipses, arrows and accented letters in
posts still arrived as UTF-8. Now nothing above 0x7F reaches an ASCII session.

**Decision 5 — substitutes keep the display width.** Width is measured once, on
the Unicode text, and every substitute fills exactly the cells its original
did. That keeps every layout rule in §3.2 and §3.6 valid unchanged.
`truncate_to_width` asks for the set's own ellipsis instead of mapping `…` to
`...`, which would be wider. Rejected: re-measuring after mapping, which would
make every width-aware screen depend on the caller's character set.

**Decision 6 — `ansi` means CP437.** That is the PC-ANSI convention, and it is
what older SyncTERM versions reported; the question after login is the safety
net for a UTF-8 terminal that says `ansi`.

**Scope.** CP437 is the only code page; SyncTERM's other fonts (CP850, CP866 and
others) are not detected or served, though the layer can take another codec
later. The CTerm device-attributes answer (`CSI = 67;84;101;114;109;… c`) would
also identify SyncTERM on any transport, but is not used until TTYPE proves too
weak. SysOp art storage with SAUCE, art with live slots, hand-drawn menu items
and animation pacing are later steps of #929 and build on this layer.

### Issue #929 — SysOp art: SAUCE and live slots — decided

Steps 3 to 6 of #929. Normative description: §3.2, "SysOp art: storage and
SAUCE" and "Art with live slots", including its list screens, hand-drawn items
and pacing.

**Decision 1 — the `.ans` file is the only source.** SAUCE, pictographs and
iCE colours are handled when the file is read. Rejected: a normalised copy in
the database with its metadata, which would give two sources of truth while
SysOps edit art files over SFTP, and would need a migration.

**Decision 2 — the art path owns the control-range pictographs.** Only art
treats 0x01–0x1F and 0x7F as glyphs; everywhere else they stay controls, so
caller text cannot smuggle control bytes in by calling them art.

**Decision 3 — iCE colours for everyone.** Blink with a background colour
becomes a bright background on every session. Rejected: switching the
terminal into iCE mode with CTerm's own sequence, which only SyncTERM
understands and would still leave UTF-8 terminals blinking.

**Decision 4 — too-wide art falls back.** Rejected: wrapping or cutting it,
which turns a drawing into noise.

**Decision 5 — plain tokens in the art, not ENiGMA½-style codes plus a theme
file.** ENiGMA½ marks views with `%VM1`-style codes and sets their size and
style in a separate `theme.hjson`. `{menu WxH}` tokens keep position, size and
style in the one file the SysOp draws, and give the console something to
check. Rejected for the same reason: region definitions in SAUCE comments,
which editors strip and nobody sees.

**Decision 6 — items that do not fit fall back to the generated menu.**
Rejected: filling the region and moving the rest behind a "more" entry, which
silently moves items a caller can use out of sight.

**Decision 7 — lists in art show a compact row.** A list region shows the
number, the name and one column per screen; descriptions and tables stay on
the generated list. Rejected: drawing the generated list inside the region,
whose tables and descriptions do not fit a drawn box and whose page arithmetic
assumes the full screen; and hiding NetBBS's own list under the art, which
shows stale rows.

**Decision 8 — hand-drawn items are found, not declared.** A drawn `[K]` is
the item, with no extra token. Rejected: a sidecar map or SAUCE comments
holding coordinates, which editors strip and SysOps cannot see.

**Decision 9 — items a caller cannot use are blanked.** Chosen over dimming
them after a mockup: blanking matches the generated menu, which hides them,
and does not advertise SysOp keys. Dimming keeps the art's full shape but
shows keys that do nothing, and relies on a colour some terminals render
faintly.

**Decision 10 — pacing on the server, not CTerm's speed sequence.** SyncTERM
can emulate a line speed itself (`CSI Ps1 ; Ps2 * r`), but bytes already sent
cannot be skipped, it works only in SyncTERM, and turning it off again queues
behind the art. Server-side pacing works on every terminal and stops at once
on a key. The skipping key is swallowed, not passed on as Voidrunner passes
its interrupting key, because a prompt follows the art. At the 5-second cap the
rest is sent at once; a cap that sped the art up instead was rejected, since
the speed is part of how the art was meant to look.

### Issue #1004 — the access map — decided

Dogfooding found that a SysOp could not tell what giving an account a level
meant without opening every board, file area, channel, door, Community and
setting, nor be sure a promotion opened nothing unwanted. Normative
description: §5.7. Step 1 (#1005) builds the map; the screens follow.

**Decision 1 — one map, held to the checks by tests.** The map is computed
from the same effective-level functions the checks use, a test compares each
gate with the real check at every threshold, and a scan of the source fails
on any level check the map does not account for. Rejected: routing every
check through the map, which would rewrite every flow's gate for no change in
behaviour; and a hand-kept list, which is the drift the map exists to end.

**Decision 2 — levels only; conditions named, grants left out.** Age,
verified-name, members-only and hidden appear as a gate's conditions, and
read or write grants are not on the map, because they belong to accounts,
not to levels. Rejected: answering "what can this account do" in the same
structure, which the change preview (#1006) does per account where it needs
to.

**Decision 3 — a write gate opens at the higher of its read and write
levels.** That is what a caller experiences. The gate still shows its own
write level and where it comes from.

**Decision 4 — the change preview is a screen, not a question** (#1006). It
shows its content with `[A]pply` and `[B]ack` in the action bar, as §3.5 asks
of every screen. Rejected: a yes/no prompt after the list, which §3.5 keeps
for irreversible or network-touching actions, and a level change can be
undone. A change that opens and closes nothing skips the screen, since
there is nothing to check. Refusals are checked first, so a SysOp never reads
a preview for a change that would then be refused.

**Decision 5 — levels run from 0 to 255.** Nothing enforced the range before:
an account could be created at, or changed to, a negative level or one above
255. Both are refused now.

**Decision 6 — level names are labels in node configuration** (#1009).
Rejected: named levels as the stored value, with gates and accounts
referring to a name, which would make renaming a level a migration of every
gate and leave Link genesis events, which carry numbers, needing a mapping.
A JSON object in the config table, rather than a table of its own, because
it is a handful of short strings read together.

**Decision 7 — a name is typed where a level is asked for on the user
screen and the Levels screen, not in the resource editors.** Those are where
a SysOp thinks in names (promote alice to Member). A resource editor's level
fields keep taking numbers, and show the name beside the value.

### Issue #992 — automatic level promotion — decided

A public node either checked new accounts by hand every day or opened
everything to brand-new accounts; ReLink ran its own cron script. Normative
description: §4.3.

**Decision 1 — checked at login, not on a timer.** A level matters only
while the account is logged in, so the login is the moment to decide, and
the caller can be told. Rejected: a periodic sweep, which promotes accounts
nobody is using and needs a task of its own.

**Decision 2 — a level set by hand takes the account out of the rules**
(the maintainer's decision). Otherwise a rule undoes a SysOp's demotion at
the next login. The account screen turns the rules back on. At upgrade only
accounts the moderation log shows demoted start marked; earlier promotions
cannot be told from ReLink's scripted ones, and are left free to climb.

**Decision 3 — logins counted on the account.** Session history keeps at
most a few rows per account and prunes them as others log in, so a quiet
newcomer's count could fall. `users.login_count` starts from the rows still
there.

**Decision 4 — the node acts, and the log says so.** An automatic promotion
is logged with no acting account ("(system)"), not in a SysOp's name: no
person made that change at that moment. Rejected: naming the SysOp who set
the rule, which is what ReLink's script did.

### Issue #993 — who may post on a Linked board — decided

ReLink Linked an announcements board, and any caller of any node carrying it
could post there: a carried post was held to nothing but the board's identity
policy. Normative description: §9.3.

**Decision 1 — origin authority, not a recommendation.** The origin chooses
who posts, and every carrying node enforces it, like closure. Rejected: a
local "accept carried posts" switch on each node, which cleans one node's
copy and leaves every other node's to its SysOp.

**Decision 2 — three modes, the origin chooses** (the maintainer's
decision): anyone, origin starts threads, origin only. "Announcements" and
"announcements with discussion" are both common.

**Decision 3 — its own event, latest wins, outside the lifecycle chain.**
Rejected: a field in `board_genesis`, which cannot change and so would only
reach boards Linked afterwards; and a chained lifecycle event, since an
origin keeps only its latest lifecycle event and a setting would push an
earlier transfer out of reach of a peer that missed it.

**Decision 4 — no capability gating.** Every node is updated before origins
set this (the maintainer's call); an older node would refuse the unknown
event type (#1022).

### Issue #632 — delivering attestations from a node nobody can dial — decided

A remote identity attestation used to travel only by being pulled from its
issuer, with a cursor, and the issuer enforced its recipient list when it was
pulled. A node nobody can dial cannot serve a pull, and most real nodes are
outgoing-only, so nothing such a node published about its callers reached
anyone. Trust objects solved the same problem by depositing at the issuer's
relays (#627); that does not carry over, because an attestation carries a
caller's verified birthdate or real name and must reach only its recipients.

**Decision 1 — a sealed, signed snapshot per recipient** (the maintainer's
choice). The issuer sends each recipient node one `sealed_attestation_bundle`:
every signed attestation and revocation that recipient should currently hold
from it, unchanged, sealed to the recipient's key (§10's `encrypt_for`) and
signed by the issuer. Its outer envelope names only the issuer, the recipient
node, a `sequence` and a time. The issuer pushes it directly when the
recipient can be dialed and otherwise deposits it at the *recipient's* relays,
named in the recipient's descriptor. A relay keeps one slot per (issuer,
recipient), in its own table so it never takes a mail slot; a newer bundle
replaces an older one. What replaces the cursor is the recipient's last
applied `sequence` per issuer: an older or equal one is refused, so a relay
cannot replay a stale snapshot. A recipient that was away needs only the
newest bundle, which is what its slot holds. Rejected: a sealed *pull*, which
still needs a dialable issuer; and a push of individual changes, which brings
the cursor back as a per-recipient ledger and needs acknowledgements to notice
a relay losing one.

**Decision 2 — a snapshot is authoritative, so removing a recipient retracts.**
Whatever a recipient holds from an issuer and the latest snapshot leaves out is
forgotten, audited as "withdrawn for this recipient". Removing a recipient
sends it a final, empty snapshot, retried until a route takes it and given up
after 90 days. This reverses the cost #596 Decision 3 accepted, that a removed
recipient kept what it held until expiry. A revocation is not used for this,
because a revocation is a universal signed statement: sent to a removed node,
it could be passed to another and retract a value still valid there, whereas an
empty snapshot is sealed to and names one recipient. The cost: a relay that
withholds a newer bundle delays a retraction by up to the attestation's 90-day
lifetime, the window §5.5 already accepts for an issuer that goes dark.

**Decision 3 — a relay learns that A sent B something, roughly how much, and
when.** The plaintext is padded to a power of two from 4 KiB, so the size says
little, and no user identifier is outside the seal. The relay is the
recipient's chosen agent, under its signed consent, and already sees more of
a letter (sender and recipient user IDs). Because a bundle can displace
another in its slot, a relay never takes one on an unverifiable claim, unlike
a letter: the deposit carries the issuer's own hello bundle, which
authenticates itself against its root key (whose hash is the issuer's
fingerprint), is merged with any chain the relay already holds so a stale one
cannot revive a compromised key, and names the key the snapshot's outer
signature must verify under. Only the issuer can fill or replace its slot; a
deposit in another issuer's name is refused, whatever sequence it claims. The
recipient still checks everything itself.

**Decision 4 — the recipient's relays, learned from its descriptor.** Sealing
needs the recipient's key and verifying needs the issuer's, and two
outgoing-only nodes never complete a hello, so both sides accept a node known
by introduction (`resolve_known_signing_key`, #627 Decision 5). A recipient
with no route — no relay published, or none the issuer has met — is a visible
per-recipient status on the issuer's Published identity screen, never a silent
drop.

**Decision 5 — one path, and no overlap.** Everything moves to the sealed
push. A bundle is sent only to a recipient, and deposited only at a relay,
whose descriptor advertises `sealed_attestations`. The plan was to keep the
pull for one release for recipients that do not; the maintainer decided on
2026-10-02 to remove it in the same release instead (issue #1046), with its
route, its signed request type and its cursor table (migration 116). The
consequence, accepted: a node on v7.14.0 or earlier stops receiving
attestations from an upgraded issuer until it upgrades; the issuer's
Published identity says so per recipient ("needs a newer NetBBS to receive
this"), and the caller's count does not include it. Rejected: keeping the
pull for dialable issuers, which would give receivers two sets of semantics
and leave removed recipients unreachable.

**Bounds.** Snapshot plaintext at most 768 KiB (sealed and encoded it stays
under the 2 MiB request limit), refused visibly beyond that; at most 4,000
objects. A relay holds bundles from at most 32 issuers per recipient and drops
one after 90 days. An issuer sends to at most 20 recipients per sync pass,
re-sends when a recipient's snapshot changes and at least every 7 days, and
uses a time-based `sequence` so a restore from backup does not move it
backwards.

### Issue #1036 — self-verifying evidence is verified — decided

`ingest_trust_objects` stored an embedded `self_verifying` signal and counted
it on its label: a configured reporter in scope could quarantine a node,
together with a second domain, with "evidence" that proved nothing.
Normative description: §12.6.

**Decision 1 — only signed equivocation reproduces.** It is the one violation
whose evidence a receiver checks against its own record of the subject's keys,
trusting nothing about the issuer. Revoked-key use, invalid authority and an
invalid signature delivery stay observer claims when they come from elsewhere.

**Decision 2 — unverified is kept, not refused.** The signal is stored and
shown as unverified, so a SysOp sees what was claimed, and re-checked once the
subject's keys are known. It counts toward no quarantine.

**Decision 3 — verified evidence counts toward the threshold, and is not
promoted to a local observation.** This supersedes the earlier §12.6 sentence
that made reproduced evidence the receiver's own observation, written for the
digest path. With automatic issuance (#589), promotion would let one issuing
node quarantine its subject everywhere; the two-domain rule keeps the
independence §12.7 is built on. Existing signals stop counting until their
evidence reproduces here (migration 112; release note).

**Decision 4 — the digest-evidence path is deleted.** `fetch_trust_evidence`,
`verify_evidence_bytes` and `activate_reproduced_digest_signal` had no
production caller, and the last promoted reproduced evidence to a local
observation, which Decision 3 rejects. Equivocation evidence always fits
inline, and fetching an issuer-named locator was network access with no
remaining use.

### Issue #914 — a compromise reaching a node that knows the signer only by introduction — decided

The Phase 4 exercise's row 9: B knew A only through R. After A rotated its
signing key as compromised, R served B a copy of A's post signed with the old
key, which B accepted, because B's bundle for A predated the rotation and still
held that key as current. Nothing failed, so nothing asked for a fresher
bundle. Normative description: §8.11, "Stale bundles".

**Decision 1 — the carrier sends the chain beside the content** (the
maintainer's choice). Rejected: refreshing introduced bundles by age, which
leaves a window of up to the refresh interval and works only if a carrier
answers an identity request; and gossiping third-party `key_transition`
events, which would overturn the rule that a transition event is accepted
only from its own subject.

**Decision 2 — introduced identities only.** A direct peer's chain comes from
the peer, by hello and gossip; a third node is never an authority over it. An
identity the receiver has never been introduced to is not created from a chain,
since a chain carries no descriptor.

**Decision 3 — merged, never replaced.** Both a carried chain and a fresher
bundle are merged into what is on file, checked against the stored root key.
Without that, a stale bundle could undo a learned compromise.

**Decision 4 — bounded, and absent when there is no news.** At most 32 chains
of at most 64 transitions; a chain holding only the first authorization is not
sent. An ordinary response is unchanged, an older requester ignores the field,
and an older carrier simply sends none. Longer chains fall back to the
stale-bundle refresh.

### Issue #672 — carriers holding copies signed by a compromised key — decided

After a compromise rotation the origin re-signs its own content, but a re-signed
object keeps its content id, and inventory diffs by content id: carriers kept
serving the old-signed copies, and the re-signed ones never spread past the
origin's own peers. Normative description: §8.11, "Stale copies".

**Decision 1 — a node that learns a compromise treats what that key alone
signed as missing** (the maintainer's choice). It stops declaring and serving
those copies, keeps their projections, and takes the re-signed copy in place
when the ordinary inventory exchange offers it. No change on the wire. Rejected:
the origin pushing its re-signed events once to each peer, which reaches only
its own peers and leaves every carrier further out, which already holds the old
copy, as stale as before; and naming the signer in every inventory entry, which
spreads through every hop but changes the inventory format of every event for a
rare case.

**Decision 2 — learned from any source.** A direct peer's revoke or hello, an
introduction, or a chain a carrier sends. A carrier also sends the chain of a
signer whose content the requester only declares, when that signer has marked a
key compromised, since a requester holding a stale copy is served nothing of
that signer's (§8.11).

**Decision 3 — bounded.** An identity's chain is looked at again only when it
has changed since the sweep last saw it, and a hello or introduction looks only
at the identity it changed, so an ordinary hello verifies nothing. Each newly
compromised key costs one look through the stored events of the kinds inventory
carries, once per process; a stale mark is stored, so a restart does not forget
it. Mail, its acknowledgements and key transitions travel outside inventory and
are left as they are.

**Decision 4 — only an event's own signer speaks for it.** A chain is any
root's to write, and nothing stops a node from authorizing another node's
public key as its own and then declaring it compromised. A copy is therefore
tested only against the compromised keys of the identity it names as its
signer (author, origin), never against another identity's; such a claim
affects nothing but the claiming node's own content. Rejecting a chain that
claims a key another identity already holds was considered and not done: the
receiver cannot know every identity's keys, so the rule could not be enforced
consistently, and attributing by signer already makes the claim harmless.

### Issue #1081 — a console resource opens on its own fields — decided

The first field test's SysOp had to press `[E]dit` on every board, area and
channel before changing anything, one extra screen on the most common path.
Normative description: §3.5.

**Decision 1 — one screen, still a draft** (the maintainer's decision).
Changes wait for `[S]ave`. Rejected: saving each field as it changes, the way
the Profile does. A resource's fields are checked together (a blank name, a
door's executable against its arguments), a Linked board's or area's name and
description travel to its peers once per save, the moderation log keeps one
entry per save, and with the cursor resting on a live field a stray Enter or
arrow key would change a resource at once.

**Decision 2 — fields by the cursor, actions by their keys.** On every one of
these screens a field letter collided with an action letter (on a board, `D`
was Description and Down, `R` Read level and Remove, `P` Pinned and Pending
posts; on a door, `D` was Description and Delete). Rejected: keeping field
letters and moving the actions to a second page, which adds a keystroke to
every action instead of removing one from every edit.

**Decision 3 — a changed draft hides the actions.** While the draft differs
from what is stored only `[S]ave` and `[B]ack` are offered. Otherwise `[U]p`,
`[L]ink` or `[S]tart service` would act on a resource whose screen shows
values that are not stored yet. It also settles the one clash between two
actions: on a door, `[S]ave` and `[S]tart service` are never offered together.

**Decision 4 — six screens, no more.** Accounts keep their screen: each key
there is a separate, confirmed operation with its own log entry, not a field
of one form. The settings screens already open into their editors, and the
network and login limits screen is a hub of six groups that would not fit
80x24 as one form.

### Issue #1082 — a minimum age can require a verified age — decided

A SysOp could not run an area that needs a verified age: every age gate
accepted a self-entered birthdate when the account had no age attestation.
Normative description: §5.5.

**Decision 1 — shaped like the name requirement.** A nullable
`age_requirement` (`NULL` or `verified`) on boards, file areas and channels,
`default_age_requirement` on Communities, and an MRC open-room setting, with
the same Community cascade. Rejected: a separate boolean, which would not
inherit the way `NULL` does, and folding the flag into `min_age`, which would
change the meaning of a stored number. Existing gates are `NULL` on upgrade, so
none changes meaning.

**Decision 2 — old enough by one's own birthdate is not hidden.** Such a
caller sees the resource marked "needs verification" and is refused on entry
with how to get verified, mirroring the name requirement. Too young, or no
birthdate, still hides it: those callers have nothing to act on.

**Decision 3 — no staff bypass.** Like the name requirement, level 255 does
not stand in for a verified age.

**Decision 4 — carried over Link as a recommendation.** A genesis carries
`default_age_requirement` beside `default_name_requirement`, omitted when
unset, so a genesis from a node that sets none is unchanged. A carrying node
stores a known value and drops anything else.

**Decision 5 — edited in the Min age field.** The editors show
"18, verified only" and take `18v`, so no editor gains a row: the area and
channel screens must still fit 80x24 whole.

### SFTP over the SSH transport — declined

Listed as a possible follow-on while issue #475 was open, on the reasoning that
SSH callers already have an authenticated connection and SFTP would ride it.
Declined; this is the decision, not a deferral.

**The motivating problem is gone.** #475 existed because most callers could not
transfer at all — Zmodem needs an emulator that implements it, and PuTTY,
Windows Terminal, an ordinary OpenSSH client and this project's own browser
terminal do not. The session-bound HTTP path (§6.2) answers that for every
transport. SFTP would not reach anyone who is currently stuck.

**What it would add is bulk and scripted transfer**, and the cost of that is a
second enforcement path for every gate the file screen applies: `min_read_level`
/`min_write_level`, `min_age`, `name_requirement`, Community inheritance,
moderation state, `get_max_upload_bytes`, and the rule that a pending upload is
visible only to its uploader and to moderators. Writes carry nearly all of it,
and would additionally have to route through `upload_file_from_temp` so that
content-addressed storage, `FILE_ID.DIZ` reading (issue #463) and Link
descriptor queueing (issue #464) behave exactly as they do elsewhere. A file
area is not a directory tree; presenting it as one means re-deriving each of
those rules in a filesystem vocabulary that cannot express them, which is how
the two surfaces drift apart.

**And bulk seeding does not want a live network surface anyway.** The realistic
case is a SysOp arriving from other BBS software with an existing collection to
bring across — a migration, run once, against a node that is not serving it yet.
That is a local job on the machine holding the files, where the work is reading
someone else's catalogue format and converting it, not moving bytes over a
protocol. It belongs in a standalone CLI tool alongside `python -m netbbs.admin`,
not in the SSH listener. Recorded as issue #505.

Revisit only if a concrete caller-facing need appears that HTTP transfer cannot
serve — not because SFTP would be convenient to have.

### Deliberately deferred without active issue

- social/M-of-N node-root recovery;
- true client-side Link-mail encryption;
- schema fingerprinting beyond SQLite `user_version`;
- Community defaults as mandatory floors/ceilings.

A deferred topic becomes normative only after an explicit design decision. Do
not infer commitment from its appearance in this list. (FidoNet/BinkP
gatewaying is tracked instead under issue #166, its own active scoping
issue, not this list.)

---

## 17. Maintaining this document

Add or change text here only when it affects:

- product semantics;
- protocol or compatibility guarantees;
- authority and trust boundaries;
- persisted data meaning;
- long-lived user or SysOp behavior;
- roadmap dependency or phase scope.

Keep one current answer per topic. Replace superseded text instead of appending
correction paragraphs. Preserve only rationale which prevents a plausible but
harmful alternative from being chosen again.

Do not add:

- numbered decision rounds;
- implementation walkthroughs;
- changed-file or test lists;
- passing-test totals;
- debugging transcripts;
- transient “next up” status;
- stale issue-resolution commentary.

Use issues for unresolved work, commit/PR descriptions for change narratives,
the engineering record for durable implementation constraints, and Git history
for archaeology.

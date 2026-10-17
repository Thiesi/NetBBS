# NetBBS design — 3. User connectivity, rendering, and interaction

This is the normative design reference for this area of NetBBS. Section numbers are preserved from the design index so existing `§n.n` references remain stable.

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
token places it. A prompt placed in the art leaves the cursor mid-screen, so
whatever answers a choice there ("Log off?", "Search:") starts on the first
free row below the art, its notices and its navigation lines, never on the row
under the prompt, which it would overwrite (issue #1083,
`netbbs.net.art_prompt`). The main menu, the welcome and logoff banners and the three
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
so Enter cannot submit an empty prompt that follows. A paced draw plays to the
end at its speed. Each banner also has a **Time limit**, off by default, or 10,
30 or 60 seconds: past it the rest goes out at once (`ART_TIME_LIMITS`). Each
piece of art plays
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
turned with `<` `>`, PgUp/PgDn or ←→ (the keys that work everywhere, §3.5).
It stays its own loop rather than becoming a
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
before review's `[E]dit body` reopens it, so the letter resumes addressed as it was
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
where 0x08 is live backspace-editing. On a terminal whose own Backspace key
sends 0x08 (SyncTERM, `terminal_detect.sends_syncterm_keys`), the byte is
Backspace on every screen, never help (issue #1119): help there is F1
(`ESC[11~`, or `ESC O P`), which every screen that answers Ctrl-H also
answers, and on-screen hints name the key that works
(`char_input.help_key_label`). Authoring help text per field is
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

A screen's breadcrumb is the way the caller came: the node's name, then each
screen that led here, then the title. No screen spells its own path. A screen
that leads to further screens names itself on the session's trail while it
runs (`netbbs.net.location`), and every title is drawn from that trail, so a
screen reached from two places is right from both, and a SysOp console
shortcut shows the shorter path it took (issue #1246). The main menu is the
root and is never a segment. The breadcrumb names a screen by its title, or
by its menu entry's word where the title is a sentence ("SysOp" for the
operations console). A path too wide for the terminal still collapses to the
title alone.

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
heading "Recent callers" / "Who has called in lately" instead of the neon
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
group taller than a page repeats its heading where it continues, and `<` `>`,
PgUp/PgDn and ←→ turn the page (§3.5), so the panel's own actions keep every
letter. A list that grows without bound
(an account's admin actions, trust configuration history) is a screen of its
own rather than the tail of another. A detail screen that keeps its own
described menu gives the menu only the rows the panel leaves, and falls back to
the packed action bar before it lets the panel's top row scroll away. The
banner and masthead screens do the same (issue #662): each fits at 40, 64 and
71 columns, and the board-list, file-area and chat-channel mastheads, which ran
three rows past 80x24 before, fit there too.

**An action bar that wraps lines its hotkeys up (maintainer request,
2026-10-09).** Packed rows started wherever the row before ended, so on a
screen too narrow for one row the keys sat at random columns and were hard to
scan. A bar that needs more than one row is now laid out in aligned columns
(`netbbs.rendering.layout.action_bar`): filled in reading order, each column as
wide as its widest entry, as many columns as fit. Aligned columns cost rows (the
user editor's bar is 5 rows at 80 columns where packing took 3), so they give
way to the packed rows when the aligned bar would take more than a quarter of
the terminal's height: 6 rows at 24 lines, 3 at 12. A paged screen with a
floor under its page (`show_detail`, the post and mail review) also packs the
bar when the page would otherwise sit at that floor and run off the screen,
and a console screen that draws a panel above its menu (`_fitted_menu`) uses
the aligned bar only if it still fits under the panel. The user editor gave
up the two rows that takes: its read-only record continues the account's
fields with no heading or blank row of its own, so at 80x24 with
redraw-in-place on its bar lines up and the screen is exactly 24 rows. A
screen that scrolls keeps the blank row its title is written after, and a
carried result line takes a row too; either way that draw falls back to the
packed bar.

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

**Keys that work everywhere** (issue #1158). A small set of keys means the
same thing on every screen that takes hotkeys, and no screen binds one of them
to anything else:

| Key | Meaning |
| --- | --- |
| `[B]` or Esc | Back. Esc first drops a highlighted row or field, if there is one. |
| `<` `>`, PgUp/PgDn | Previous / next page of a list, a long text or a form's sections. |
| ←→ | The same paging on lists and long texts. On a form they step the highlighted field's value instead. |
| `/` | Find, wherever a list can be searched. |
| `?`, F1 or Ctrl-H | Help for this screen. Every hotkey screen has some. |
| Enter | Choose the highlighted row or field. |

`B` is the only letter in the set; every other letter is free for the screen's
own actions. Paging is never on a letter, so `[N]ew scan`, `[P]rofile` and
`[P]ost` keep theirs, and a list never asks the caller to tell "next" from
"newer". Line prompts are not hotkey screens: there `/` starts a command where
one is offered, and Esc keeps the value (above). Doors draw their own screens
under their own contracts and are outside this rule. A test keeps it: no
screen's hotkeys bind a reserved key to anything but its meaning here.

**A menu key is its label's first letter** (issue #1158, step 2). On a menu,
an action bar or a one-key question, every letter key starts the label it
belongs to: `[R]ecent callers`, never `P[r]evious callers`, and never a later
word's first letter (`Reply [a]ll`) or a key set apart from its label
(`[U] Read`). Where two labels on one screen start with the same letter, one
of them is reworded with a synonym; a key is never bracketed further in. A
toggle names its setting and shows the current state after a colon,
`[F]ollow: off` / `[F]ollow: on`, so the key and the label stay the same
whichever way it is set. A label that is an explicit action (`[T]urn off`)
is not a toggle label and may stay. Settings and field screens are outside
this rule: their fields are numbered (below). A test keeps it.

**A field is chosen by its number** (issue #1158, step 3). On every draft
field editor -- the Profile, Name & details, the console's settings and
editors, and a resource's own screen -- each field is drawn with a two-digit
number in front of its label, `01` to `99`, and the number is read like a
list's row number: two digits, or one digit and Enter. The numbers run
straight through a screen's sections, so on the Profile `13` is Color depth on
every page, and typing a field's number from another section turns to that
section and opens the field. ↑↓ and Enter still choose the highlighted field,
and ←→ still step its value. Fields have no letters, so a screen's letters
belong to its actions: `[S]ave`, `[B]ack`, and a resource's `[U]p`,
`[R]emove` or `[L]ink`. The action bar offers the range, `[01-24] change`,
where a row of field letters used to be, which also makes a form shorter.
With menu descriptions on, the highlighted field's description is shown under
the list, where the field letters' descriptions used to be. A resource's own
screen names the range in the key hint it already has (`01-12 or ↑↓ choose`)
instead, and shows no description row, so it is no taller than before. A handbook names
a field by its label, not its number, because a number moves when a field is
added.

A key a caller can press is highlighted wherever it is offered, not only on
menus (issue #974): a prompt that lists its choices in running text
("Unsaved changes. [S]ave, [D]iscard, or [C]ancel?") and a detail-panel label
that names a key ("[R]otate") colour the bracketed key as a menu entry does
(`highlight_hotkeys`). Issue #1083 extended this to outcomes and reports: a
notice's "Use [P]review to verify it looks right." highlights its key too, in
the notice helpers themselves (`notices.announce`, the console's
`_announce_line`), and a report line (a `[C]heck` verdict, an art's size) puts
its numbers in the emphasis colour (`highlight_report`). A saved file's path
reads in the value colour, and a SAUCE credit shows its title and its artist
(author/group) as separate fields. Only help pages still write keys plain. A
test keeps prompts and menu labels from writing a bare `[X]word` again.
An outcome says how it went with a mark in front and keeps the usual colours
for the rest (issue #1109): a green `✓` for a success, a yellow `!` for a
warning, a red `✗` for a failure, then the text in the normal colour, its keys
in the menu key's green and a path in the value colour (`status_result`). A
whole line in the success green had turned the scheme around on every
confirmation (white keys on green labels, where everywhere else keys are green
and labels white). An outcome that changed nothing stays a muted line with no
mark, and good news (new mail) keeps its own colour. The notice helpers apply
this to every queued line, including one already written all in a status
colour (`colored("Upload failed: ...", fg_color=ERROR_COLOR)`). The marks have
stand-ins on classic and ASCII terminals: `√` and `x` in CP437, `*` and `x` in
ASCII.
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
written before the last one. A type-the-name confirmation answered with
anything other than the name is not a silent cancel: the next screen says
what was typed and that nothing happened, as a warning (issue #1119); only a
blank answer is the plain "Cancelled.". Like every outcome, it is carried into
the screen drawn next, never written under that screen's clear.

**A console resource opens on its own fields** (issue #1081). The six resource
screens of the SysOp console -- a Community, a category, a message board, a
file area, a chat channel and a door -- are one screen each, not an overview
with an `[E]dit` key in front of the draft editor. The screen shows what can't
be edited first, in a compact header of one or two rows (counts, place in the
callers' list, Link status), then the editable fields with the cursor already
on the first one, then the actions. Long Link details (origin, closure, pending
transfers, peer reach) form a "NetBBS Link" section after the fields. A field
is chosen by its number or by the cursor, as on every field editor (above):
↑↓ to choose, Enter or Space to change it, ←→ to step a value. Fields have no
letters, so the action keys (`[U]p`, `[R]emove`, `[P]ending posts`, `[L]ink`,
`[S]tart service` and the rest) keep theirs. Changes go into a draft as on every other editor; nothing is
stored before `[S]ave`. Once a field differs from what is stored, the action
bar shows only `[S]ave` and `[B]ack`, so no action runs against values the
screen no longer shows. `[B]ack` leaves at once while nothing has changed. With
changes it discards typed work, which can't be undone, so it is the
hotkey-chosen, destructive action the yes/no rule above allows a question for,
the same "Discard unsaved changes?" every draft editor asks. On a Linked
resource this node is not the origin of, a field the origin controls is shown
in place, read-only, labelled "set by origin" (today no field is: a node
edits its own presentation of a carried resource). Creating a resource uses the
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
flows unwind back to. Every draft editor shows the session's pending outcomes
by default, so a field's own prompt -- Profile's "Display name cleared.", a
refused date -- is read on the redraw that follows it (issue #1124). Plain
outcome text is classified the same way on both sides
(`notices.announce_line`): a success gets `✓`, a failure ("Could not …",
"Not a valid …") `✗`, a "Cancelled." or "No …" stays muted. A flow shared
with a live stream -- a Link live message or a direct-chat invite, which chat
runs inline -- takes a `report` callable: chat writes the outcome into its
stream, the Who screen announces it for its redrawn list. The Verify screen
and the Who screen no longer hold a result behind `[Enter] Continue`.

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
happened. Every list numbers its rows this way, `01` to `99`: the picker, a
board's posts, the mailbox and a file area (issue #1158,
`netbbs.net.row_numbers`). The three that draw their own screen used to take
one digit, `1`-`9`, so a row past the ninth had no number to type.

A whole word typed at a one-key prompt ("Communities", "no") acts on
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

A Linked board, file area or chat channel, one shared with other nodes over
NetBBS Link, has its name drawn in `LINKED_COLOR` (issue #1104) instead of the
accent every other name takes. It costs no column and no row. Where colour
can't say it, the name ends in `LINKED_MARKER` (` ~`) instead, kept whole when
the name is cut to fit: for a caller reading plain ASCII, and on a row inside
SysOp list art, whose rows take the art's own colour. The list's Ctrl-H says
what the colour or marker means, on a list that has a Linked entry. The
SysOp's own resource lists use the same colour.

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

  The rest of the gate set is not shown in lists. Level and age already
  decide visibility rather than needing to be displayed, so the open
  question is only whether a caller should be told *why* something is
  absent -- a different feature from telling them why something present
  will refuse them.
- **A gated resource names its gates on entry** (issue #1105). Opening a
  board, file area or chat channel that restricts anyone shows one line
  under its title, in `GATE_COLOR`: `Requires: age 18+ verified · verified
  name · level 20+ to post`. It lists only gates that restrict someone, with
  the values that apply after the Community cascade -- the age (in the
  access map's words, `describe_age_gate`), the name requirement, the read
  and write levels above 0 (a write level no higher than the read level adds
  nothing and is left out; a file area's verbs are browse and upload), and a
  channel's level and members-only. An ungated resource gets no line. Every
  caller who can open the resource sees it, a SysOp included, and a Linked
  resource shows the gates this node applies. It is one row at the session's
  layout width, cut with the session's ellipsis, with ` - ` between gates
  for an ASCII caller (`netbbs.gate_summary`). A gate the caller does not
  meet is drawn in `ERROR_COLOR`, and an ASCII caller also reads
  ` (not met)` after it (issue #1115): the same line says why they can read
  a board but not post to it, so no second "Read only" line repeats the
  level.
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

**A typed field is edited where it is drawn (maintainer request,
2026-10-09).** With redraw-in-place on, choosing a field that takes typed
input (by its number, or the cursor and Enter) redraws the screen with the
field highlighted, puts the cursor in its value and a short hint ("Enter
saves, Esc cancels") on the `Choice:` row; a refusal is said on that row or
carried into the next draw. A field never draws a prompt of its own below
`Choice:` and never asks a yes/no question before the typing: a new account's
password is typed unseen into its field and once more to confirm, its public
key pasted into its field, an empty answer clears either. Field editors get
this from `edit_resource_draft`; a screen with a cursor of its own says where
it drew the value (`netbbs.net.resource_editor.editing_in_place`): the
account detail for Level, Display name and Birthdate, and the review after
writing a post or mail for To and Subject. Fields that open a picker or a
list of their own (a trust anchor's node, the MRC room blocklist) are not
typed fields, a multi-line text opens its editor, and the level ladder's
`[N]ame` and `[G]o to level` act on a list row and keep their prompt. A
screen that scrolls, one taller than the terminal, or redraw-in-place off
still opens the prompt below `Choice:`, since rows counted from the top are
then not where the value is.

---

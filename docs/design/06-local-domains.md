# NetBBS design — §6. Local product domains

Part of the [NetBBS design](../NetBBS-design-doc.md). Section numbers are stable: cite this area's rules by their section numbers (§6.n).

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
- Fits as many rows as the terminal holds and pages with `<` (older) and `>`
  (newer), PgUp/PgDn or ←→ (§3.5); `[R]ecent` jumps to the newest page.
- Has a cursor: Up/Down, and Enter or a digit to open a post.
- Follows §3.6: display-width columns. Below readable width, a row becomes
  "subject -- author". Author gives way to subject first, because the reader
  shows the author in full.
- Has a header that says where the caller is and what they can do: the path
  they came through (Community, "Message boards", category), newest or older
  posts, how many were new on arrival, and "Linked" or "linked from X".
- Shows the board's description and, for a caller who can read but not post,
  why: the gate they do not meet is marked on its "Requires:" line (issue
  #1115). Only a reason that is not a gate -- a Linked board whose origin
  keeps posting to its own callers -- still gets a line of its own.
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
  page boundaries. These move to another post, not another page of this one,
  so they are the screen's own actions rather than the paging keys of §3.5.
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
yet", in §6.6's terms) and an "about" column that leads with a name-gate note
before the description. A Linked board shows by its name's colour (§3.6).

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

- **Cross-posting** (issue #1298, decided in §16): a new post, prose or art,
  can go to several boards at once from `[T]o boards` on its review screen. It
  is written as one ordinary copy per board, each with its own content id,
  replies, moderation, pinning, removal, edits and withdrawal, tied by the
  nullable `posts.crosspost_group` on each root (a bare id here, `<id>@<home
  node fingerprint>` for carried copies). The picker lists the boards the
  caller may read, the current one ticked and fixed; a board they cannot post
  on -- level, verified name, age, closed, the Link posting setting, or no
  color for an art post -- is muted with the reason and cannot be ticked. A
  node-wide cap, 5 by default and set in the console's Limits & retention,
  counts the current board; the picker refuses a tick past it with a line
  naming it. At `[P]ost` every board is checked before any copy is written
  (`require_write_gate`, `posting_refusal`, closed or hidden, the length
  limits with a Linked board's file lines counted), and the copies are
  written in one transaction; a refusal names the board on the review screen
  and posts nothing. A reply or an edit is never cross-posted.

**Polls** (issue #1299, decided in §16; built by #1332, `netbbs.boards.polls`,
`netbbs.net.poll_view`). A poll is a post with side tables keyed by its root
post: `board_polls`, `board_poll_options` and `board_poll_votes`. The subject is
the question and the body introduces it.
- **Asking:** `[N]ew poll` on a board's post list (not on an FTN board) opens a
  poll draft editor -- the question, 2 to 10 answers, one answer or up to N, a
  closing date 1 to 365 days ahead (7 by default), and whether results show
  after voting or only after the close -- then the body editor and the review
  screen, where `[Q]uestion and answers` reopens the poll. Nothing is stored
  before `[P]ost`.
- **Changing:** the answers, the choice mode and the results setting lock at the
  first vote, and on a Linked board when the poll is posted. The closing date
  stays editable through `[E]dit` while the poll is open; a closed poll stays
  closed. A text edit is an ordinary revision.
- **Voting:** `[V]ote` on the reader, for anyone who may reply there, opens the
  answers as a draft with the caller's vote ticked; `[V]ote` stores it, and it
  can be changed until the poll closes. A held poll takes no votes until it is
  approved. The guest account holds one vote, which a later guest call cannot
  change (#1075).
- **Results:** after voting, or at once for a caller who cannot vote, or only
  after the close when the author chose that; before then the poll says when.
  From 60 columns one row per answer (label, bar, count, percentage of voters),
  two rows below; `█`/`░`, or `#`/`.` with Unicode style off. Screens show
  totals and the number of voters, never who voted what, and say how many
  votes were not counted (§9.2).
- **Closing and removal:** `[C]lose poll` for the author and the board's
  moderators. A withdrawal or a takedown closes the poll and hides its totals.
  Deleting the post (a rejection, the expiry sweep, a deleted board) deletes the
  poll and its votes through `forget_orphaned_polls_without_commit`, beside
  #842's file references; a deleted account's vote goes with it.

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
are all "no description", never a failed upload. The same sandbox opens FTN mail bundles in
these formats (§6.8, "Mail bundles").

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

Chat rings the caller's bell for private messages and, if they choose, for
lines naming them: a per-caller level (`off`, `private`, the default, or
`mentions`) set in Profile → Communication or with `/bell`, a per-session
rate limit, and a tone in the web client. §16, "Issue #1302 — audible chat
bells", is the normative answer (issue #1333 built it). The bell is decided
by an explicit flag on what the hub delivers and written as its own `\x07`
after the line, so no line, transcript or replay carries it.

Chat's input row -- in a channel, an MRC room and a direct chat -- scrolls a
line wider than the terminal within the row, with `<` and `>` marking text out
of view, as every other prompt does since issue #546 (issue #926). The row is
the terminal's last, outside the scroll region, so a soft wrap there has no row
to go to and overwrote the line. Tab completion edits the line and the window
redraws, and the repaint after an incoming message or a status update draws the
same window around the cursor, not the head of the line.

A resize keeps the conversation on screen. When the height changes, chat clears
the screen to rebuild its scroll region and pinned rows at the new size, and
then draws again the newest lines it has shown, rewrapped at the new width, as
many as the content area holds. It keeps the last 500 lines for this, with
their colour but without cursor moves, clears or the bell. Before, the clear
left the content area blank: a caller who maximized the window saw the status
line move and the chat vanish.

A line from MRC is marked by an `[MRC]` badge in front of it, and its sender is
`nick@site`: the nick in its MRC color, the `@` muted, the site in a color of
its own that a linked node never uses (`MRC_SITE_COLOR`). Stored rows keep
`user@site (MRC)`; screens drop the suffix, since a parenthesis after a name
holds an account (§16, issue #899): `/names` and Find say `(on MRC)`, and
`/who` says `on MRC` in the column beside the name.

Chat `/help` is a width- and height-aware command table. It aligns descriptions
in one column, colors command names, parameters, and descriptions as distinct
fields, and paginates within the scrolling region while preserving chat's three
pinned rows. The bare list remains permission-aware; `/help <command>` may
explain a command outside that list without granting permission to run it.

Every other command that answers with more than a line -- `/list`, `/who`,
`/names`, `/whois`, `/finger`, `/members`, `/bots`, `/mrc` -- answers with one
block in the scrolling area: a rule in the rule color with the title in the
header color set into it and, at its right end, the count of what it lists.
Rows follow one space in, their columns aligned and the last one wrapping under
itself; prose (a comma-separated roster, a closing note) wraps at the same
indent. The block is at most 78 columns wide. Flat lines read as a wall of text
between conversation; a titled block is told apart from it at a glance. A
one-line answer (a refusal, "No one is here.", a toggle's new state) stays one
muted line. A cell may be several colored pieces, and the blocks use the
colors chat already gives each thing: in `/who` the caller's own name in the
self color and other local callers in the accent color, "here" in the success
color and an away message in the warning color, an MRC `nick@site` and "on
MRC" in the MRC site color, a linked node's name in the node color; in
`/list` the MRC and linked-node counts likewise. A block taller than the chat
area is paged the way the hub's answers are (below).

`/list` counts everyone this node can see in a channel, not only its own
callers: `2 here · 3 on MRC` for a bridged room, from the room's roster while a
caller here is in it and from the hub's room listing otherwise (marked with its
age when it is not current), and `4 on Vertigo` for a linked channel's callers
on other nodes (`5 on 2 linked BBSes` when they are on several). A subscriber
knows them while a caller here holds a live subscription -- the roster goes with
the last one -- and the origin knows every subscriber's (§8.10.2, issue #1281).
`/who` names each one's node.

The hub's answer to an `/mrc` question carries no request id and no end
marker, so the reply lines that follow the question (its first within ten
seconds) are collected until the hub has been quiet for a second, then drawn as
one block titled `MRC hub · /mrc <question>` with the time the answer began.
Its rows keep the hub's own colors and alignment and carry no badge or time of
their own -- except the answer to `/mrc info`, whose `Label: value` rows are
drawn like our own blocks' rows: the label in the label color, the value in the
value color, a slash-separated value (the platform, the version) one color per
part with the slashes muted. Only rows of that shape are redrawn; the hub's
frame lines, and any layout the hub changes to, keep the hub's own look. Other traffic is shown as it arrives meanwhile; a second question
asked before the first answer went quiet gets its own block. An answer taller
than the chat area is paged (the hub may push 300 lines at once, and `/mrc
bbses` names every board on the network): each page ends `page 1 of 4 · PgDn
more · PgUp back · Esc done`. PgDn, or Enter on an empty line for a terminal
that sends no PgDn, shows the next page; Esc drops the rest; any other line
does too. The pager is not modal -- unlike `/help`'s, it reads no key of its
own, since the answer arrives while the caller's input line already owns the
keyboard: the line editor hands PgUp, PgDn and a bare Escape to chat through a
callback inside its keystroke's critical section, and the line being typed is
untouched. A reply line arriving once its answer is drawn stands alone with its
`[MRC]` badge, as a CTCP answer always does.

PgUp and PgDn also scroll back through the chat itself, a page at a time over
the 500 lines chat keeps for redrawing after a resize. A terminal's own
scrollback is no use in chat: the next redraw of the pinned rows jumps it back
to the bottom, a second later at most. Scrolled back, the chat area holds
still, so it can be read or copied: lines that arrive meanwhile are kept and
counted rather than drawn, and its last row says `8 lines back · 1 new · PgDn
newer · Esc live`. Only the pinned status and input rows keep updating. PgDn
past the newest line, Esc, Tab completion's candidate list or sending a line
returns to the live chat, drawn again with whatever arrived. A resize does too.
Scrolling needs the pinned rows, so a terminal shorter than four rows has none;
PgUp at the newest line of a pager's answer scrolls back over the pages already
shown.

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

**Chat bots.** A bot is an external program speaking as `<Name>.bot`, never
taken for a person; its design is §16, "Issue #1208 — chat bots", and its
protocol is `docs/NetBBS-bot-api.md`. What a caller sees of one:
- **Asking privately.** `/msg Baud.bot <text>` sends one request only the bot
  sees, and `/private Baud.bot` sends every plain line to it until `/close`,
  as `/private <user>` does with a person; `Baud.bot@Node` reaches a bot on a
  linked BBS. Such a request is never recorded, broadcast, carried over Link,
  relayed to MRC or seen by bots that read the channel, and its answer is
  always private. Its channel is the caller's current one, so the bot must be
  allowed there and every grant, mute and `/bots` setting applies (§16,
  issue #1372). Tab after `/msg ` or `/private ` offers, after the people
  online, exactly the bots that would answer there: this node's running
  bots with an addressed grant, and the linked bots the channel's roster
  shows whose node can answer privately, as `Name.bot@Node`; muted, hidden
  and stopped bots are left out. `/whois`, `/finger` and `/invite` complete
  people only.
- **Private answers.** A bot may answer any request privately. The answer is
  a bot line marked `(to you)`, shown only to the requesting session, kept
  in that session's lines like `/msg` and never in the channel's history,
  search or replay, and it never rings. A private answer is implied by the
  `addressed` grant and has its own fixed per-caller limit, 30 an hour per
  bot, instead of the channel's. Over Link it travels only in the bot's own
  result frame, never as a direct message or a channel event, and it is
  refused rather than made public when the caller's node cannot take it
  (§16, issue #1372).
- **Titled blocks.** A bot's answer may be a titled block drawn like `/who`:
  `[BOT] Name.bot · <title>`, up to 12 rows of an optional label and a text
  of up to 200 characters, costing one line of the channel's hourly limit.
  The host builds a plain fallback line from it, which older nodes show and
  search matches. A private block's title ends `to you`. A live block taller
  than the chat area is paged; join replay draws blocks whole, unpaged. Reminders,
  announcements and other unprompted lines stay one line (§16, issue #1373).
- **Door game results.** A bot granted a door as an event source may announce
  what callers earn there -- a rank, a top-tier achievement, a season's
  medals, a capture, a perfect round -- in a line NetBBS words, such as
  `Nib reached Merchant Captain in Voidrunner.` `!top <door>` answers the
  door's own top ten. Both name only callers whose profile switch "Game
  results announced in chat" is on (the default), and only for doors every
  caller may play (§16, issue #1378).
- **Lines held for a caller.** A bot may ask the host to say a line when one
  caller next joins the channel; the host holds it in its own table, with an
  expiry, and says it only in that channel, once per join of the account.
  Baud uses this for reminders whose owner is not there when they fall due
  (held 7 days) and for `!tell` notes (held 14 days, local recipients only,
  the block list checked at creation and again at delivery, §6.4) (§16,
  issues #1375 and #1376).
- **What a bot is told.** Its `hello` carries the node's display timezone,
  so times are read in the node clock, lists which of its channels are
  Linked, and names the node as other BBSes address it. A message event says whether the caller is a moderator of the
  channel or the guest, and whether a private answer can reach them (§16,
  issues #1372, #1375 and #1379).
- **Help desk.** With model conversation on, the default help-desk mode
  answers "how do I..." from excerpts of the caller handbook, shipped as
  package data and chosen per request by a keyword score, and from node facts;
  the host ends the answer with the section it used and a "may be wrong"
  hint. A public answer names only boards and areas everyone can read; a
  local caller's private answer may name what that caller can see. Companion
  mode is plain chat (§16, issue #1382).
- **Writing and trying bots.** NetBBS ships a stdlib helper,
  `netbbs.bots.botkit`, that owns the handshake, result correlation and
  state; Baud is written on it, and a second example, Tally, ships installed
  and off. The bot editor's **Try** and the Chat bots list's `[T]ry selected`
  run a bot against a scratch database, state and channel, showing every
  host event and bot action, its results and a bounded stderr excerpt; model
  actions are rehearsed, never sent (§16, issue #1374).

**Paging the SysOp.** `/sysop <reason>` pages the node's staff from any
channel, MRC rooms included, with or without a bot. The reason never enters
the channel, its history, Link or MRC; the caller is answered with a private
titled block saying who was told or that the page waits, and the away notice
where it applies. A bare `/sysop` shows its usage. Baud's help names the
command, and a `!sysop` line, which is already public, gets Baud's pointer to
it. The queue and its limits are §5.6, "Pages" (§16, issue #1377).

**Scheduled announcements.** Beside the midnight, New Year and clock-change
lines (§16, issue #1354), the SysOp can schedule announcements per channel:
once on a date, daily, weekly on chosen weekdays, or monthly on the first to
fourth of a weekday, at a time in the node clock, with 1 to 160 characters of
text. Each is said as a muted system notice, `Announcement: <text>`, stored as
the local `daybreak` kind, and recorded even when nobody is in the channel. A
missed slot is skipped. A channel holds at most 8 rules, and rules less than
an hour apart are refused on Save. In a Linked channel an announcement reaches
other nodes only after the SysOp approves that channel for announcements; it
then travels as a signed channel message authored by the SysOp who saved the
rule, marked as an announcement (§9.6), and is said on this node only if that
account is no longer a usable SysOp. The rules are edited in a draft editor
under Content > Chat channels (§16, issue #1380).

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
above its prompt, because a direct chat clears the screen on its way out. The
Who screen never pauses either: an invitation that ends without a chat is
announced above its redrawn list (issue #1124).
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
  before #925 they were the one live path that did not. A bot's `!tell`
  note (§6.3, issue #1376) is checked the same way, with the sender the host
  took from the request: at creation, where the sender is told, and again
  when it is delivered, where a block made since drops it silently.
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
  caller) and system notices never pass the check. A page to staff (§5.6)
  is addressed to the node's staff, not to a person, and is not covered.
  Nor are a bot's quotes and polls, or the oneliner wall (§6.9): they are
  public like a channel, and deletion and moderation are the tools there.

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
- Fits as many rows as the terminal holds and pages with `<` `>`, PgUp/PgDn
  or ←→ (§3.5). A mailbox is bounded (§14), so the whole folder
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
  Send until `[E]dit body` shortens it (issue #812), as for any over-limit letter.
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

Carrying a Link Community carries its present and future member resources,
while retaining visible per-resource and whole-Community local exclusions.
Origin defaults are recommendations; carrying-node overrides win locally. The
rationale is §16, "Issue #1308 — Link Communities"; the rules are these.

- **Members.** A Link Community lists only its origin's own linked boards,
  chat channels and file areas: those whose *current* origin is the node that
  signs the Community. Doors are not linked. A member whose origin moves away
  (§9.4) drops out on every carrier.
- **Events.** `community_genesis` (`origin_fingerprint`, `community_id`,
  `name`, `created_at`, optional `description` and `default_min_read_level`,
  `default_min_write_level`, `default_min_age`, `default_name_requirement`,
  `default_age_requirement`); `community_members` (`community_id`,
  `origin_fingerprint`, `members` -- exactly `boards`, `channels` and
  `file_areas`, each at most 500 distinct ids -- `created_at`, `nonce`); and a
  terminal `community_closure`. All three are signed by the origin's current
  signing key and verified against the origin, so a carrier relays them but
  never authors them. `community_id` is content-addressed from the origin, the
  name and the time, and a receiver refuses a genesis it does not match. A
  membership list replaces the whole membership and is not chained: the newest
  `created_at` (ties by content id) wins, and superseded lists are not kept. A
  list or closure for a Community whose genesis has not arrived waits for it,
  and a member id whose own genesis names another origin is ignored.
- **Promotion.** A local Community is promoted from its SysOp screen. The
  genesis carries its name, description and defaults, with a membership list
  of the members that are already linked and originate here: promotion links
  nothing, so filing a private board under a Link Community never publishes
  it. Linking, unlinking or moving such a member, by any path, is answered by
  a new list on the next sync pass. Unlinking or deleting the Community signs
  its closure; the origin keeps serving the genesis, last list and closure so
  the closure reaches every carrier, and the Community is local again.
- **Subscription.** A received genesis is always *offered* (§9.3), with its
  origin and member count, whatever the node-wide intake does for boards:
  subscribing is a standing rule a person makes. Accepting the offer is the
  subscription. From then on each member the Community lists -- when the
  subscription starts, and each one new to a later list -- is accepted as if
  the SysOp had accepted its offer, within the per-type carry caps; one past a
  cap waits offered with reason `cap`, and an offer already in front of the
  SysOp is not silently taken later (§16, issue #561, Decision 4). Excluding a
  member keeps it out. Deleting the copy is leaving the Community: it is
  recorded as excluded, accepts nothing more, sheds no member, and Restore
  subscribes again.
- **The local copy.** The subscription materializes a `communities` row marked
  as Link, with its origin, under the genesis name or, if a local Community has
  it, #671's suffixed one. Screens show the linked marker (§3.5, issue #1104)
  and the origin's label. The origin's defaults are stored on the copy and its
  SysOp may edit them; an edit is local. Carried members go into the copy as
  they are carried, and already-carried members with no Community move in when
  the subscription starts. A member a SysOp files under a Community has been
  placed locally (`community_placed_locally`), and no later membership list
  moves it, in or out. A member moving in drops the explicit level 0 a carried
  board or file area is given when its genesis names no level, so it inherits
  the Community's defaults. A member the origin removes moves back out and
  stays carried.
- **Closure.** A carrier keeps every member. Its copy becomes a plain local
  Community, marked as closed at its origin, which its SysOp may keep or
  delete; an offer of a closed Community is withdrawn.

Transfer of a Community's origin, Link-blanket and Community-blanket moderator
grants, following a Community, and linking doors remain unbuilt (§16, issue
#1308, "Not done").

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

A cross-post (issue #1298) is one message on several boards. Opening one copy
marks the copies the caller can see on this node read too
(`record_post_opened` on each), on boards the caller has visited: a board
never visited keeps having no baseline, rather than having its backlog
counted read by a visit elsewhere. New scan counts a group's unread copies once,
on the first board in its list that has one. A reply is never cross-posted,
so "replies to you" already lists each reply once. `search_posts` lists it once, by the first copy the caller can
open. A copy's byline says "also in X, Y", naming only the boards of the
other copies the reader can open.

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

### 6.8 FidoNet-technology networks (issue #166)

A node can join FidoNet or an FTN othernet (fsxNet, AgoraNet, Micronet, ...)
as an ordinary FTN node. The node carries echomail on boards and netmail in
Mail. This is a **gateway at the boundary**. Link's protocol, events and trust
model do not change, and FTN content never enters them. Decisions and the
alternatives rejected for each are in §16, "Issue #166". Stage A of the FTN
round (§16, "Issue #1154", tracker #1461, planned for v7.36) adds file
echoes, FileFix and the robot rules, the nodelist from a file echo, echo-list
import, unknown echoes and the held-item actions. They are described below as
they are to be built, and so are stage B's netmail extras: file attach,
crash and hold flavours, and bounces (#1470-#1472), and stage C's file
requests (#1475). Stage D's points and the hub's records, sessions and
transit netmail (#1478, #1479) are under "Points and downlinks". The
round's later stages are listed under "Planned" at the end of this
section.

**Networks.** A SysOp adds one network record per FTN network:

- the network's name and 5D domain;
- this node's address in it (`zone:net/node`, or `.point` for a point);
- the uplink's address and its BinkP `host:port`, and whether it is called
  over TLS;
- the session password, plus optional packet and AreaFix passwords;
- the robots' names, `AreaFix` and `FileFix` by default, and an optional
  FileFix password, which falls back to the AreaFix password when empty;
- the TIC password for file echoes;
- the nodelist echo and the nodelist's file name, with an optional diff file
  name;
- whether netmail no account here can take is returned to its sender (on by
  default), the file area files sent with netmail are stored in (none by
  default, which holds them), and whether held netmail is given to a caller
  without a password (off by default);
- the poll interval, and whether this node answers incoming calls.

Every network is off until the SysOp enables it, and settings take effect
without a restart, as MRC's do (§6.3).

**Transport: native BinkP, both directions.** The node speaks binkp/1.1
(FSP-1024) in-process, with multiple batches, falling back to binkp/1.0
(FTS-1026) with a mailer that doesn't offer them, and with CRAM-MD5 (FTS-1027)
and NR mode (FTS-1028) for receiving. Two roles:

- **Originating.** The node polls each network's uplink on its interval, at
  least once a day, as hubs ask, and calls out sooner when mail is waiting.
- **Answering.** The node listens on a configurable port (24554 by default)
  for an uplink or a direct caller. The port is one for the whole node, and
  the node listens only while some enabled network answers calls.
  - **Secure sessions.** Only a session whose address and password match a
    configured link is *secure*, and only a secure caller is handed that
    network's waiting mail. A caller presenting an uplink's address with the
    wrong password is refused.
  - **Unsecure sessions.** Packets from an unsecure session are held for the
    SysOp rather than tossed.
  - **Limit.** At most a few calls run at once; another caller is told the
    node is busy (`M_BSY`).

**IPv6** (issue #1467). The node dials a host name over IPv6 or IPv4,
whichever answers first (RFC 8305 "happy eyeballs"). An IPv6 address is
written in brackets with a port, `[2001:db8::1]:24554`, in the uplink's host
and in a nodelist's `IBN` and `INA` flags (FTS-5004); a port written into the
uplink's host becomes its port. The node answers on every address, IPv6 and
IPv4, unless the SysOp names the addresses (`ftn_listen_host`, one or more IP
addresses): in the network editor's Transport section, or with
`python -m netbbs.admin ftn-listen --host`. These settings and the answer
ports are node-wide, like the answer port.

**BinkP over TLS** (issue #1468). FTN has no certificate authority of its
own, and the hubs that offer BinkP over TLS ("binkps", usually port 24553)
mostly use self-signed certificates. TLS therefore adds privacy on the wire
and nothing to authentication, which stays CRAM-MD5 and the secure/unsecure
rules above, unchanged inside TLS.

- **Calls out.** A network's **Call over TLS** is off, *pinned* or
  *verified*:
  - **Pinned** is trust on first use, as SSH does it. The first call that
    completes stores the hub's certificate's SHA-256 fingerprint, and a later
    call meeting another certificate is refused before any BinkP frame moves,
    with the new fingerprint in the error. The SysOp clears the pin after the
    hub renews its certificate, or pastes the fingerprint the hub published.
    Changing the uplink's host lets a learned pin go.
  - **Verified** checks the system's certificate authorities and the host
    name, for a hub with a public certificate.

  Direct netmail calls stay plain: a nodelist says nothing of TLS.
- **Calls in.** An optional TLS answer port beside the plain one, with a
  certificate file and an optional key file the SysOp names (NetBBS's web
  listener has no certificate of its own: it sits behind a reverse proxy).
  A caller's certificate is never asked for. When the files can't be loaded,
  the plain port goes on answering and the status screen says why.

Rejected: verifying against certificate authorities by default, which fails
against nearly every hub's self-signed certificate; and TLS without any
check, which a man in the middle defeats as easily as no TLS. CRAM-MD5 proves
this node to the hub, not the hub to this node, so the pin is what tells a
caller it reached the hub it reached before.

**Multiple batches** (issue #1469). A session is binkp/1.1 multi-batch when
the remote's `VER` says binkp/1.1 or later, or it sends `OPT MB`; this node
always sends both. A batch is over once both sides have sent `M_EOB` and
every file sent in it is confirmed. In multi-batch mode a batch in which
either side sent a file is followed by another, and the session ends after a
batch in which neither did. Before each batch after the first, the session
asks a hook (`batch_hook` on the mailer and the listener) for more files,
telling it what the remote sent since: file requests (below) are answered
this way, in the same call. A remote that claims binkp/1.1 but hangs up
between batches, with nothing outstanding, has completed the call. Every
bound holds across batches, per session: files, bytes, the idle, handshake
and session timeouts, and at most 64 batches.

ND/NDA, binkd's no-dupes mode, is never offered, so it is never in force. It
is not an FTSC document (FTS-1029 is data-frame compression, FSP-1024's ND
section is empty), and its guarantee rests on both sides keeping, across
sessions, a record of each file whose transfer is not yet settled -- state
this node does not keep, and which an implementation tested only against
itself could get wrong in the direction that loses mail. Duplicates are absorbed as before: a message
is marked sent only on `M_GOT`, and the tosser's dupe check drops a repeat.
ND waits for interop tests against a real binkd (#1493).

**A call that fails part way** keeps what moved before it failed. Each file
received whole was confirmed to the remote, which let go of it, so it is
tossed; a packet the remote confirmed marks its messages sent. The status
screen shows the failure with what moved.

No external mailer or tosser is needed or supported. Packets are Type 2+
(FSC-0048) when written, and Type 2, 2+ or FSC-0039 are accepted when read.
Bundles are identified by content rather than by name. Outbound mail is
queued in the database, modelled on §13.7's work items, not in BSO
directories.

**Mail bundles** (issue #1477; §16, "Issue #1154", Decision 16).

- **ZIP** is opened in-process.
- **LHA, ARC, 7-Zip and RAR**, and ARJ, are opened by the sandboxed external
  tools that read `FILE_ID.DIZ` (§6.2), shared as `netbbs.files.extract`.
  Installing a tool is the opt-in, as for DIZ reading:
  - LHA: `lha`, else 7-Zip;
  - ARC: `nomarch` (7-Zip has no ARC support);
  - 7-Zip and ARJ: `7z`, `7za` or `7zz`;
  - RAR: `unrar`, else 7-Zip.
- **How a tool reads a bundle.** The tool lists the archive first. ARC has no
  tool that lists reliably, so its 29-byte headers are walked instead. Then
  each member is read from the tool's stdout, one run per member, and
  nothing is extracted to disk. The tool runs in a private temporary folder
  under the DIZ sandbox: rlimits on POSIX, no shell, bounded output, and a
  kill and reap on every exit path. For ARC, `nomarch -p` prints every
  member, and the output is split by the sizes the headers give.
- **Limits.** A ZIP's limits apply: 256 members, 64 MiB unpacked in all, and
  members returned by base name. Two more apply to a tool-read bundle:
  - a member must come out at the size the listing gives, because 7-Zip
    prints nothing, and still succeeds, for a name it can't find;
  - a member name that isn't printable ASCII, contains `*?[]`, starts with
    `-`, has no file name, or is a link refuses the whole bundle. A tool
    could read such a name as a wildcard or an option, and FTN bundles hold
    8.3 names, so nothing real is refused.
- **Without a tool, or when one fails,** the bundle is held whole, waiting
  for a TIC, with the reason. A missing tool's reason names the tools and
  their pkgsrc packages. [R]elease on a held file opens it as a bundle, so
  the SysOp can install the tool and then toss what waited.
- **Nodelist archives** in these formats are opened the same way, on the
  database lane, which a nodelist's rare arrival can afford.
- **Outbound.** A network's **Bundle outgoing mail** setting (`off`, the
  default, or `zip`) puts the session's packet into a ZIP. The ZIP is named
  by FTS-5005: the net and node differences in hex, the weekday, and a
  sequence character (`00000063.we4`). The sequence moves on per bundle and
  is kept per network, so two calls on one day don't reuse a name. Messages
  are marked sent only when the remote confirms (`M_GOT`) the bundle.

**Outbound files** (issues #1470 and #1471). Files wait for a session in a
queue of their own, beside the queued messages, and every session takes
both from one place, `netbbs.ftn.outbound`. The queue is general: a
netmail's attached files, hatched files with their TICs and file requests
use it.

- **A queued file** is a file area entry, read from its stored content, or
  a file on disk. It is sent under a name BinkP's safe-name rule leaves
  alone, on a route (`uplink`, or `direct` to a node, a point by its boss)
  and a flavour, as a message is.
- **Once it is sent**, its row goes. A file on disk can be kept, deleted or
  cut to nothing (BSO's `^` and `#`), but only in the node's own spool
  folder; a file area entry is always kept.
- **Bounds.** At most 200 files wait per network, and more are refused. A
  session carries at most 256 MiB of them, the same bound as what a session
  receives, and leaves the rest for the next.
- **A file that is gone** (deleted, unapproved, or over 64 MiB) when its
  session starts is dropped with a node-log line, and the netmail's sender
  is told by a letter from the BBS.
- **The session interface.** For one node (the uplink, or a direct node),
  `outbound_for_node` gives the waiting messages as one packet and the
  waiting files. The mailer and the listener pass them to the BinkP
  session, then settle with the names the remote confirmed (`M_GOT`): the
  messages are marked sent and the files' rows removed. A file is read in
  chunks as it is sent, so nothing waits in memory before a session.

**Flavours** (issue #1471) apply to messages and files alike:

- **Normal**, the default: direct when the nodelist lists the node, else via
  the uplink, as "Routing" below says.
- **Crash**: direct to the destination, at once, never via the uplink. It
  needs a BinkP address for the node in the nodelist, unless the node is
  the uplink. After 5 failed calls the netmail is dropped, and its sender
  is told why in a letter from the BBS.
- **Hold**: nothing calls out. It goes in the next session with its node
  that a password proves: the uplink's, or a point's or downlink's (below).
  A session with any other node proves nothing
  about who is at the other end -- anyone can present an address -- so it
  carries no held mail, unless the network sets **Give held mail to callers
  without a password** (off by default). Then a node that calls in, or that
  this node calls for other mail, is handed its own held items, and a
  caller that only claims the address is handed nothing else. Held mail
  still waiting after 30 days is dropped, and its sender told.

**Points and downlinks** (issues #1478 and #1479; §16, "Issue #1154",
Decisions 15 and 17). A network's SysOp can serve systems under this node,
each a **link** on the network's **Links** screen (**Settings ▸ Echomail
& netmail (FTN) ▸ [L]inks**, a picker with [C]reate, [D]elete and a draft
editor per link):

- **A point** has this node's address with a point number, `86:549/40.3`.
  It calls in for its mail and is never called. A node that is itself a
  point has no links.
- **A downlink** is a node this node feeds, `86:549/41`. It calls in too
  (*hold*), or, given the host it answers BinkP on, this node calls it
  when mail waits (*poll*), at most once per 5 minutes and after a failed
  call again after 1, 2, 4 ... minutes. A downlink's own points are
  reached through it. Its mail never goes via the uplink, which would
  only route it back.

Each link has a session password, required, and optional packet,
AreaFix, FileFix and TIC passwords (the last three serve the AreaFix
server and TIC forwarding, below, and are not yet in the editor), a
bundle preference (bare packets or ZIP), and a status: *active*;
*paused*, set by the link's own `%PAUSE`, which stops its echomail but
not its netmail; or *disabled* by the SysOp. The Links screen shows each
link's last call and what waits for it, and the FTN status screen counts
them per network. Nothing is a link until the SysOp adds it: on a network
that forbids feeding other systems, nothing changes. nixNet allows a node
to feed approved points only (policy III.8), so ReLink can serve points
there and stays a leaf.

- **Sessions.** A caller presenting a link's address must prove that
  link's session password, on either answer port; a wrong one or none
  ends the call. Only then is the link's mail read and handed over -- of
  every flavour, since the link's password proved it -- in a packet to
  its address under its packet password, and what it sends is tossed as
  the link's, its packets checked against that packet password. A
  disabled link must still prove its password, so nobody else can take
  its address, but it is given nothing and what it sends is held. **Give
  held mail to callers without a password** never applies to a link.
  Each call, in or out, is recorded on the link. A link's file request
  ("File requests") is answered in that session as a secure link's, and a
  disabled link's is not; a request made here to a point or downlink
  waits in its outbound for its next proven session.
- **Its outbound.** Anything queued for a linked address -- a caller's
  netmail, a bounce, transit netmail, files -- waits for that link
  alone, whoever queued it: the queue looks the address up as it queues.
  At most 2,000 messages and 50 files wait for one link, and more is
  refused. What a link has not collected after 30 days is dropped and its
  sender told, as held mail is. Deleting a link drops what waits for it
  the same way. Netmail to a point this node does not serve is refused.
- **A point's echomail** is tossed here, through the board's moderation as
  any echomail is, and then sent to the uplink as this node's own: SEEN-BY
  this node and the uplink, PATH this node, and whatever SEEN-BY and PATH
  the point's software wrote dropped, since a point is never in either
  (FTS-0004, FSC-0074). Its MSGID, Origin line and kludges go on as
  written. On a moderated board it goes once the post is approved, and is
  forgotten after 30 days unapproved (at most 1,000 wait per network). A
  point's echomail for an echo no board carries waits as an unknown echo
  and goes on when the SysOp creates or maps the board; one the SysOp
  releases from the held store goes on too, by the address it was held
  under. Echomail or netmail a point sends must say it was written at the
  point's own address (its Origin line, else its MSGID; its INTL and
  FMPT, else its packet), or it is held: a point sends its own mail.
- **Transit netmail**, for another node, is routed with a `^AVia` line
  naming this node (FTS-4009): to the point or downlink it is for, else,
  when a point or downlink sent it, to the uplink. INTL, and FMPT and
  TOPT where points need them, are added when the sender left them out,
  since the next system reads zones and points from the kludges, not
  from the packet this node writes. The message is otherwise forwarded
  byte for byte, marked in transit. Netmail for another node that came
  from anyone else, the uplink included, is not this node's to route: it
  is counted and dropped, as before. A netmail whose Via lines already
  name this node (a loop), that passed 30 systems, that is addressed back
  to the link that sent it, or that is for a point this node does not
  serve, is held for the SysOp; a repeat is dropped by its MSGID. Files
  attached to a transit netmail are not forwarded: they wait in the held
  store as unclaimed files and expire.

Not built yet, and the subject of the hub's second part (#1479): echomail
fan-out to points and downlinks, the AreaFix/FileFix server and TIC
forwarding. They build on these records without a schema change: each
link's echoes and file echoes go in `ftn_link_areas` (with a read-only
mode), one encoded message is queued for one link with
`enqueue_for_link_without_commit` (a copy of one post for several links
names the link in its reference), and SEEN-BY and PATH are merged by
`netbbs.ftn.route.merge_seen_by_path`. Until then a downlink's echomail is
tossed here and goes no further, and a TIC from a link is held (check 3
below).

**What a session received** is sorted once the session ends (issue #1458):

0. A `.REQ` file is a file request (below): answered during the session or
   refused, and never held or tossed.
1. `.TIC` files first (file echoes, below).
2. A file a TIC names, in this session or waiting from an earlier one, is a
   file-echo file. It is never opened as a mail bundle.
3. Bare packets are tossed.
4. Archives are opened as bundles (ZIP, and LHA, ARC, 7-Zip, RAR and ARJ
   through the external tools, above). An archive with no `.pkt` member is
   held whole, as a file that may still be waiting for its TIC. So is one no
   installed tool opens. A bundle's non-packet members are held one by one.
5. Anything else is held, waiting for a TIC -- unless it answers a file
   request made here (below), when it is held as requested and never
   expires.

Nothing a session received is dropped without being held or counted. A file
from an unsecure session is held as it came, unopened, before any of this.

**Echomail on boards.** A board is either local, Linked or FTN; it cannot be
two of these. One echo area maps to one board, and the SysOp sets the mapping
per board. Nothing is mapped by default.

- *Inbound.* An echomail message becomes a post on its board:
  - **Author.** It is labelled `Name (zone:net/node[.point])`, never with `@`,
    which names a Link identity. It has no local account and no fingerprint.
  - **Moderation.** It goes through the board's own moderation (§5.3). It is
    never trust-evaluated (§12): FTN has no identity a node can verify.
  - **Threading and date.** `^AREPLY` threads it under the post carrying the
    matching `^AMSGID`. The message's own date, corrected by `^ATZUTC`, is
    kept as the post's date.
  - **Duplicates.** A message whose MSGID (or, without one, content hash) was
    seen on that area before is dropped.
  - **Unknown echoes.** A message for an area no board carries is held as an
    *unknown echo* (a "bad echo" in tosser terms), under its network and tag.
    The **Unknown echoes** screen, reached from
    the FTN status screen with **[U]nknown echoes**, shows one row per tag with its message count and
    when it was first and last seen. Its actions:
    - **[C]reate** opens the new-board editor with the tag filled in. Saving
      maps the board and re-tosses the tag's messages.
    - **[M]ap** picks an existing local board that carries no echo, maps it,
      and re-tosses.
    - **[D]elete** drops the tag's messages.

    A re-toss goes through the ordinary tosser, with the board's moderation,
    duplicate checks and threading. Unknown-echo messages are kept apart from the
    held store: at most 1,000 per tag and 10,000 or 64 MiB overall. The
    oldest are dropped first, and the drops are counted on the status screen.
    A board is never created without the SysOp (§16, "Issue #1154", Decision
    9).
- *Outbound.* A local post on an FTN board is exported to the uplink:
  - the `AREA:` line, `^AMSGID` (a persisted serial, unique across restores),
    `^AREPLY`, `^APID`, `^ATZUTC` and `^ACHRS`;
  - a tear line and an Origin line;
  - SEEN-BY and `^APATH` as FTS-0004 sets them.

  **Origin line.** It is ` * Origin: <text> (<address>)`, within FTS-0004's
  79 characters. The text is the board's own Origin when it has one (issue
  #1474, §16 "Issue #1154", Decision 10), else the network's, else the
  BBS's name. The SysOp sets a board's with **[O]rigin (FTN)** on the
  board's screen, offered while the board carries an echo; an empty line
  goes back to the network's. A board's Origin is bounded by this node's
  address on its network, so that the whole line fits, and a longer one is
  refused rather than cut. The network's text is cut to fit as before.
  Making the board local again, or moving it to another network's echo,
  clears its Origin.

  A copy of a cross-post (issue #1298) is its own message with its own
  `^AMSGID`, and its first body line is `* Crossposted in <ECHO>, <ECHO>`,
  naming the other echoes of the same network that get a copy. It names no
  local board, Linked board or other network's echo, and is left out when
  there is no other echo on the network.

  **From name.** A post goes out under its author's username, except on a
  **real-name echo** (issue #1443): a board whose effective name requirement
  (§5.5, its own or its Community's) is `verified_and_displayed` sends the
  author's verified real name instead. That board alone does; the same
  author's posts on any other echo, on Link and in chat keep the username.
  The name is the one frozen on the post when it was written (§5.5, issue
  #1453), which is also what the board shows, so a post a moderator approves
  later goes out under the name it was written under. A reply there is
  addressed to the parent's frozen name when the parent is a local post, so
  its To matches the From the network saw.
  A post that could not go out under a verified real name is refused before
  it is written: the author has none, or it is longer than the From field's
  35 bytes in the character set the message is written in. The board page
  says why in place of `[P]ost`, the cross-post picker marks the board, and
  every way of writing a post meets the same check -- a reply, a cross-post
  copy, a bot's post, and a door's, whose label is nobody's name. It applies
  to a local SysOp too, whom the name gate itself lets through. A post that
  reaches the exporter without a usable name anyway (written before the board
  displayed names, or carried from elsewhere) stays local with a node-log
  warning, as for a full queue. It is never sent under the username or with the name cut short.

  A post that arrived from FTN is never exported again. SEEN-BY and PATH on
  tossed messages are kept intact, so loop detection works.
- *Edits and deletions* have no FTN equivalent. A local edit is not
  re-exported, and removing an FTN-sourced post removes only the local copy.

**Netmail in Mail.** Netmail is personal mail (§6.4) with an FTN address:

- **Addressing.** A caller writes to `Name (zone:net/node[.point])` on the
  To line. It is the same form an FTN author is shown with on a board, and
  replying to an FTN post or netmail fills it in. The compose screen names
  the network and says that netmail is not private: every system it passes
  through can read it.
- **Senders.** Each network has a minimum level for sending netmail. It
  defaults to SysOp only until the SysOp lowers it, because netmail goes out
  under the node's address. No level gates receiving it.
- **Routing.** A netmail goes **direct when possible**. The node processes
  the network's nodelist (FTS-5000) and calls the destination over BinkP when
  its entry lists a BinkP host (`IBN`/`INA` flags). Otherwise the netmail
  goes to the uplink for routing, and so does one whose direct call fails
  after its retries. How the nodelist arrives is under "Nodelist" below.
  That is the **Normal** flavour; **[F]lavour** on the review screen also
  offers **Crash** and **Hold** (above), to anyone who may send netmail.
- **File attach** (issue #1470). A netmail the SysOp sends carries the files
  it points at (§6.4's file refs, attached the usual way). It goes out with
  FTS-0001's FileAttach attribute and the files' names as its subject on
  the network, and each file is queued on the netmail's route and flavour.
  The Sent copy keeps the subject that was typed and points at the files.
  A file may be at most the node's upload limit and BinkP's 64 MiB, the
  limit a file echo's file meets (§16 "Issue #1154", Decision 4); two files
  under one name, or names longer together than the 71-byte subject, are
  refused. Anyone else's netmail names its files in text, as a letter to
  another BBS does.
- **Kludges.** INTL, FMPT and TOPT (FTS-4001) are written whenever zones or
  points need them.
- **Inbound delivery.** Netmail to `SysOp` or `Sysop`, in any case, always
  reaches the SysOp, even when an account has that name (issue #1472).
  Other netmail is delivered by matching the To name,
  case-insensitively, against a username, or a name an account was renamed
  away from: addressing uses the canonical identity (§6.3), never a chat
  alias. Failing that, it goes to the one account whose verified real name
  it is, compared exactly but for case and surrounding spaces -- the name a
  real-name echo sent (issue #1443). An unverified display name never
  matches.
- **Bounces** (issue #1472; §16 "Issue #1154", Decision 26, which amends
  #166's Decision 7 and #1443's Decision 2). A netmail matching no account,
  a verified real name two accounts share, or an account that takes no mail
  goes back to its sender, on a network whose **Bounce unknown** is on, as
  it is by default. The reply is a netmail from `Mailer-Daemon` at this
  node's address to the sender's name and address, with `^AREPLY` to the
  original, the reason, and the original's first 10 lines quoted. With
  bouncing off, such netmail goes to the SysOp, marked as addressed to
  someone else. A loop guard keeps a bounce from starting a loop. Nothing is
  bounced
  - from a robot's name (AreaFix, FileFix, the network's own robot names,
    `Mailer-Daemon`, `Postmaster` and the like), so nothing is bounced to
    one;
  - that is a bounce itself, by its subject (`Undeliverable:` and the
    like);
  - from this node's own address;
  - to an address that got 5 bounces from this node in the last day.

  Such a netmail goes to the SysOp instead, with a line saying why it was
  not returned, and so does one whose bounce the full outbound queue can't
  take. A bounce is queued as an ordinary netmail, under the same bound.
- **Inbound attaches** (issue #1470). A netmail with FileAttach names its
  files in its subject; a folder part the sender's system wrote is cut off,
  and a name is checked by the rules a TIC's `File` meets. When the netmail
  is delivered, each name waits for its file, and the file that arrives
  under that name in a secure session, in that session or another, is
  stored in the network's attach area through the area's own moderation,
  showing the sender as its uploader. The letter points at it with a file
  ref (§6.4), as at any file here, and opens with a line naming the files.
  - The file is held for the SysOp instead, with the reason, when the
    network has no attach area (or it is removed, hidden or Linked), a file
    of that name is already in the area, or it is over the limit above.
  - A file that comes first waits in the held store as one waiting for its
    TIC does, and is claimed when its netmail is delivered. A name still
    waiting after 7 days is forgotten.
  - A file from an unsecure session is held unopened, as everything from
    one is, and is never claimed by a netmail later released from the held
    store: the SysOp [S]aves it by hand.
  - A bounced netmail's files are not kept.
- **Robots.** AreaFix and FileFix requests are netmail too. **[A]reaFix** and
  **[F]ileFix** on the FTN status screen (**Node ▸ Echomail & netmail (FTN)**)
  ask for a request and send it to the uplink's robot of that name, under the
  robot's password. The robot's reply arrives as netmail in the SysOp's Mail,
  not on the console. The same rules apply to both robots' requests:
  - `+TAG` (or a bare `TAG`) links an area, and `-TAG` unlinks one.
  - The known `%` commands are checked: `%LIST`, `%QUERY`, `%LINKED`,
    `%UNLINKED`, `%AVAIL`, `%HELP`, `%RESCAN`, `%PAUSE` and `%RESUME`. All
    but `%RESCAN` take nothing after them, so a word that follows one is the
    next command. `%RESCAN` may name a tag, followed by the robot's own
    arguments.
  - Any other command starting with `%` is sent verbatim, as one line. It is
    never turned into a `+%...` link request (issue #1459), and `+%...` or
    `-%...` typed as such is refused.
  - The request is typed on one line. `%RESCAN` and a verbatim command take
    the words after them as their arguments, up to the next word starting
    with `%` or the end of the line.
  - `%PWD`, `%PASSWORD`, `%PKTPWD` and `%TICPWD` are refused. `%TICPWD`
    is FileFix's counterpart of `%PKTPWD` and is refused for the same
    reason: each changes a password this node stores. The reason names
    **Change robot password**, a Networks action: the new password is typed
    masked, twice, then sent to the chosen robot as `%PWD`, under its current
    password, and stored for that robot in the same step. If the robot
    refuses, its reply in Mail says so, and the SysOp sets the old password
    back in the network editor.
    - Only the robot that was sent `%PWD` changes. A FileFix with no password
      of its own keeps the old AreaFix password as its own when AreaFix's
      changes, since the hub heard only about AreaFix's, and the outcome
      says so.
    - Session, packet and TIC passwords are agreed with the hub's
      coordinator and stay editor fields.
  - A robot's name is 1-35 printable ASCII characters, the To field's 35
    bytes.
  - A request holds at most 50 commands.

**File echoes (TIC, FTS-5006).** A file area can carry one file echo tag of
one network. Its SysOp sets this on the area's screen, the way a board's echo
is set. An area is local, Linked or FTN, never two (§16, "Issue #166",
Decision 3, extended in "Issue #1154"), so an FTN file never gets a Link
`file_descriptor`. Nothing is mapped by default. A file arrives as a pair: the
file itself and a `.TIC` that names it. The pair is checked in this order, and
the first check that fails holds both halves with its reason. There are two
exceptions: at check 7 only one half exists, and it waits as "Pairing" below
says; at check 10 a duplicate is dropped and counted, not held.

1. The session is secure. Otherwise both halves are held unopened, as above.
2. The TIC parses: `Area`, `File` and `Crc` are present, and the TIC is at
   most 64 KiB.
3. `From` is the network's uplink. Until the hub (stage D), no other system
   sends this node file echoes.
4. `Pw` matches the network's TIC password, compared case-insensitively. With
   no TIC password set, every TIC is held.
5. `Area` is mapped to a file area of this network, or is the network's
   nodelist echo (under "Nodelist" below). Any other tag is an unknown echo:
   unknown file echoes get their own rows on the Unknown echoes screen, where
   **[C]reate** opens the new-file-area editor and **[M]ap** picks a local
   file area. Either one then runs the tag's held pairs through these checks
   again, from check 6, the way echomail is re-tossed. A pair that passes
   arrives by TIC, so later `Replaces` values can match it.
6. `File` is a path-safe name, under the rules for a SysOp upload's name
   (§6.2): no folder part, no leading dot, no control characters or colon,
   and at most 255 bytes. A long name in `Lfile` or `Fullname` is used
   instead when it passes the same rules.
7. The file has arrived (pairing, below).
8. The file's length equals `Size` when `Size` is given, and is within both
   the node's `max_upload_bytes` and BinkP's 64 MiB per file. A larger file is
   held.
9. Its CRC32 equals `Crc`.
10. It is not a duplicate. A file with the same name and CRC32 already
    received by TIC in that area is dropped and counted.
11. No other file of that name is in the area, unless `Replaces` covers it.

A pair that passes goes into the area through the area's own moderation
(§16, "Issue #1154", Decision 1). In a moderated area it waits for approval,
as an upload does. Its description comes from the `LDesc` lines, else `Desc`,
else the archive's `FILE_ID.DIZ` (§6.2), and is normalised as every
description is. The file is recorded as arrived by TIC, with its network,
tag, origin address and CRC-32. It has no local uploader, and the listing
shows the network and the TIC's `Origin` address in place of a username, as
`nixNet (86:549/1)`.

- **Pairing.** A half whose partner hasn't arrived waits in the held store,
  with the reason "waiting for its .TIC" or "waiting for `<file>`". It is
  paired as soon as the partner comes, in any later session with the same
  network. A TIC alone that failed one of checks 3-6 waits the same way,
  with that reason, and the pair is checked again from the start when its
  file comes. A half still unpaired after 7 days is deleted, with a node-log
  line. A TIC that doesn't parse names no file: it is held, but neither
  pairs nor expires.
- **Replaces.** Once the new file is visible (when stored in an unmoderated
  area, or on approval in a moderated one), each `Replaces` value removes the
  files that match it. Only files in the same area that arrived by TIC can
  match, never an upload by a caller or the SysOp, and never a file in another
  area. A value is a plain file name, compared case-insensitively, and `*` and
  `?` match within the name. A value containing `/`, `\`, `:` or `..` is
  ignored, with a node-log line. The new file never replaces itself.

**Hatching** (issue #1476; §16, "Issue #1154", Decision 14). A SysOp sends
one of this node's own files into a file echo with **[H]atch** on the
listing of a file area that carries one, acting on the file under the
cursor and confirmed with one yes/no, since it goes to the network.

- **The TIC** (FTS-5006) is written with CR LF lines, in CP437 when every
  character fits and UTF-8 otherwise. It has `Area` (the area's tag),
  `Origin` and `From` (this node), `To` (the uplink), `File`, `Lfile` when
  the name is not 8.3, `Size`, `Date` (when the file was stored, in Unix
  time), `Desc` (the description's first line, else the name) and an
  `Ldesc` line per description line, `Created`, `Crc` (CRC-32 of the stored
  bytes), one `Path` line (this node, now), `Seenby` for this node and the
  uplink, and `Pw`, the network's TIC password. Control characters in a
  description become spaces, so no value can start a line of its own.
- **Names.** A name that is already 8.3 of BinkP's safe characters is sent as
  it is. Any other is sent under up to six letters and digits of its stem,
  two hex digits of the long name's CRC-32, and up to three of its last
  extension, upper case (`NETBBS4F.ZIP`), with `Lfile` carrying the real
  name. The TIC gets a fresh random 8.3 name.
- **Queued** to the uplink through the outbound files queue, normal
  flavour: the file area entry (always kept), then the TIC, written to the
  node's spool folder and deleted once sent. The two travel together: when
  one of them can't be sent at its session (the file was deleted or
  unapproved), the other is dropped with it and the spooled TIC removed.
- **Once.** Each hatch is recorded (`ftn_hatches`: the file, network, tag,
  names, CRC-32, size, who, when) and audit-logged on the area. A file
  already hatched into a network is hatched again only when the SysOp
  confirms that on purpose; that TIC carries `Replaces` with the earlier
  copy's names. A name containing `*` or `?` is left out, since receivers
  read `Replaces` as a pattern; the 8.3 name never contains either.
- **Refused**, with the reason and nothing queued: a file not approved; a
  file that came over FTN (by TIC, as a netmail's file, or saved from the
  held store, which records its sender for this), so nothing echoes back;
  an area with no file echo; a network with no TIC password; a file over
  BinkP's 64 MiB; a name a receiver would take for a control file (`.TIC`,
  `.PKT`, `.REQ`); a network with fewer than two free places in its files
  queue.
- **Auto-hatch** is a file area setting, off by default and on upgrade,
  toggled with **[A]uto-hatch** on the area's screen. With it on, a
  caller's upload is hatched once when it becomes approved: when it is
  stored in an unmoderated area, or when it is approved in a moderated one.
  An automatic hatch that is refused is a node-log line; the upload and the
  approval stand.

**File requests** (issue #1475, §16 "Issue #1154", Decision 13). A
request is a WaZOO `.REQ` file (FTS-0006): a line per file, a name or a
pattern with `*` and `?`, optionally followed by ` !password` and by an
update word (`+time` newer than, `-time` at most as new as, in Unix
seconds). It travels as an ordinary file, named as FTS-5005 names a request
to its node: net and node in hex, `000C0002.REQ` for 1/2.

- **Making one.** **[R]equest files** on the FTN status screen is a draft:
  the node to ask, the uplink by default, and the names, which may include
  the magic names below and `!password` after a name. [S]end writes the
  `.REQ` into the node's spool folder and queues it on the outbound files
  queue, crash: it goes at once, never via the uplink to someone else, and
  the file is deleted once the remote confirms it. Only the uplink, or a
  node the nodelist lists as answering BinkP, can be asked; a second
  request to a node whose first still waits goes under a random hex name.
  A request dropped by the queue (its crash calls kept failing) says so on
  its record.
- **What comes back** is received like any other file. A file that is not
  mail and that no TIC or netmail claims answers the request when the
  session carried a request of ours to that node, or when its name matches
  an open request to it within 14 days (a magic name matches the names its
  answer usually has: `FILES*`, `ALLFILES*`, the nodelist's name). It is
  held, as requested, without the 7-day expiry of a file waiting for its
  TIC, and the SysOp [S]aves it to a file area. It still waits for a TIC
  all the same: a file echo's file that happens to match (the nodelist
  echo's list, after a request for `NODELIST`) pairs with its TIC when that
  comes, as any file does. Held rather than stored
  at once: the remote chose its name and content, and a file area entry
  needs an uploader and a description that a SysOp vouches for -- the same
  reason a file from an unproven session is held. From a session without
  a password it is held unopened, as everything from one is, with the
  request named in the reason.
- **Answering** is off by default, per network: with no settings row, or
  with **Answer requests** off, a `.REQ` is refused. When on, a `.REQ`
  received in a binkp/1.1 multi-batch session is answered in the session's
  next batch, through the batch hook. **A binkp/1.0 session is not
  answered**, and its request is not queued for a later session: it is
  refused with a node-log line. Queuing would keep a stranger's request
  as state until a call that may never come, and an answer in a later
  session goes to whoever then presents the address, which proves nothing
  without a password.
- **Who may ask:** secure links only (a session password proved the
  caller: the uplink, until stage D), the default, or anyone.
- **What is served:** the files of the file areas the SysOp offers, with
  **[O]ffer to file requests** on the area's screen, a toggle stored by the
  area's content-addressed id. An area is served only while any caller
  could read it -- no read level, age gate, verified-name requirement or
  hidden Community -- and is checked again at each request, so an area that
  gains a read level stops being served. A Linked area can't be offered:
  Link content never enters FTN. An area carrying one network's file echo
  is served only on that network. Only approved, unexpired files are
  served, under BinkP's safe form of their name. The magic names:
  - `FILES`: a listing of every file a request could get, area by area,
    generated as `FILES.TXT` (CP437, at most 4 MiB);
  - `ABOUT`: a file on the host the SysOp names, not served when none is
    named;
  - `NODELIST`: a file the SysOp names, else the network's nodelist as this
    node kept it, named `<list name>.<day>`; **Serve NODELIST** off refuses
    it.
- **Safety.** A line with a folder part, a drive colon, `..`, a leading dot
  or anything outside printable ASCII is refused, and so is a pattern of
  wildcards and dots alone: the listing is `FILES`. A pattern matches only
  the files above, with the linear matcher `Replaces` uses, so nothing
  restricted is enumerated. Offered files are public, so a password is not
  checked; it is never stored, logged or shown. A file deleted or gone
  since it was listed is skipped. A `.REQ` is read up to 16 KiB and 50
  lines.
- **Limits:** files and bytes per session (10 and 50 MiB by default, at
  most 100 and 256 MiB), and files and bytes per requesting node per day
  (25 and 100 MiB, at most 1,000 and 1 GiB), counted from the records of
  the last 24 hours. Whatever the settings, a network answers at most 1,000
  files and 1 GiB a day: an unproven caller can claim any address. A name
  over a limit is reported as such, and a file already sent in the session
  is not sent twice.
- **Logging.** Every request, answered or refused, is a node-log line with
  the node, whether the session was secure, the names and the outcome
  (sent, not found, over the limits, refused), and a record kept 30 days
  (at most 2,000 each of requests made and received, so a flood of
  received requests never evicts the requests made here). The FTN status
  screen shows, per network, whether it answers, how many areas are
  offered, and its last five requests, made and answered.

**Held items.** The held store keeps packets, files and TICs, each with its
network, sender, size, time and reason. The **Held** screen lists them, and
its actions are:

- **[R]elease** re-tosses an item as from a known system. It works on packets
  only. For a file it says why it can't (issue #1460).
- **[S]ave to file area** picks a file area and stores the file there as the
  SysOp's own upload, through that area's normal path. A file held together
  with its TIC keeps the TIC's description. A name already in the area is
  refused.
- **[E]xport** asks for a path on the host and writes the file there with
  the rules for SysOp uploads (§6.2): written beside the destination and
  renamed, never over an existing file, no symlink followed, and
  audit-logged. A folder as the path writes the file into it under its own
  name. The item stays held until it is deleted.
- **[D]elete** throws the item away.

The store holds at most 200 items and 256 MiB, enough for a file at the
64 MiB limit. What doesn't fit is lost with a node-log error and counted on
the status screen, as before.

**Nodelist.** A network's nodelist (FTS-5000) arrives in one of two ways:

- **By hand:** [N]odelist import on the FTN status screen, or
  `ftn-import-nodelist`, from a file on the host: the list itself, or a ZIP
  holding it (the network's `<name>.<ddd>`, or the one list the ZIP holds).
- **From the nodelist echo**, automatically. A file that arrives by TIC in the
  network's nodelist echo and passes the TIC checks is handled by its name:
  - `<name>.<ddd>`, or an archive of it, is a full list;
  - `<diff name>.<ddd>`, or an archive of it, is a diff.

  An archive's name may end in the day number, in FTS-5000's archive letter
  and the day's last two digits (`NODELIST.Z89`), or in `.ZIP`. The names
  are compared case-insensitively, and a file of any other name is not the
  nodelist's.

  An archive is recognised by content and opened within the 64 MiB nodelist
  limit. ZIP is opened in-process, and LHA, ARC, 7-Zip, RAR and ARJ through
  the external tools that open mail bundles (issue #1477). Without the tool,
  the file is held with the reason. The nodelist echo needs no file area. When it is
  also mapped to one, every file in it is stored there as well, and a file of
  any other name is stored only then. Otherwise such a file is held.

What happens to each kind:

- **A full list** has its CRC-16 checked against the first line, as FTS-5000
  specifies: computed from the second line to the end, before the end-of-file
  byte, as if every line ended CR LF. A list from the echo whose first line
  carries no check value fails the check. On a mismatch nothing is imported,
  the file is held, and the current list stays in use. On a match the list
  replaces the stored entries, and its raw text is kept, one per network.
- **A diff** (FTS-5000's `A`, `C` and `D` commands) is applied to the kept
  raw text. Its first line must equal the kept list's first line, and the
  result's CRC-16 must match. Then the result becomes the kept list and is
  imported. If any of this fails, nothing changes: the diff is held, and the
  status says the network is waiting for a full list. That is the fall-back
  (§16, "Issue #1154", Decision 5). The next full list ends the wait. A diff
  whose result is the kept list already (its full list came first) is
  skipped, without a failure.
- **A list imported by hand** is kept as raw text too, so later diffs apply
  to it. Its CRC-16 is checked when its first line carries one, and a
  mismatch is refused; a hand-made list without a check value is imported.

The FTN status screen shows, per network, the list's day number, when it was
imported and how (full, diff or by hand), its node count, its verified
CRC-16, the nodelist echo and names, whether it is waiting for a full list,
and the last failure.

**Echo lists.** A hub's list of echoes, `.NA` for echomail or `.FA` for file
echoes, is imported from a path on the host, from the FTN status screen. The
file holds one `TAG description` per line, with `;` comments. It may be up to
1 MiB and 10,000 lines. It is read as UTF-8 when it decodes as such, else
as CP437. Tags are normalised as a board's echo tag is. Invalid lines, a tag
starting with `%`, and a tag listed twice are skipped and counted. The
import is a draft editor (§3.5):

- whether the list is echomail or file echoes, from the extension (`.FA` is
  file echoes, anything else echomail), which the SysOp can switch;
- a checklist of the tags and their descriptions, where tags already mapped
  on that network are marked and can't be checked;
- the Community the new boards or file areas go into.

**[S]ave** creates one board (for `.NA`) or file area (for `.FA`) per checked
tag in that Community. Each is named from the description, cut to 40
characters at a word where it can be, or from the tag when there is no
description or that name is already taken, and is mapped. Then one AreaFix
or FileFix request with `+TAG` for each tag is queued, split into requests
of at most 50 commands. Nothing is written before [S]ave. A save that can't
create everything, an echo whose description and tag are both names
already taken for example,
writes nothing and keeps the draft with the reason. Creating a board
commits, so a failure after the first one was made removes what was made
before the draft is shown again.

**ROT-13 in the reader** (issue #1473, §16 "Issue #1154", Decision 12). The
post reader and the reader of a received letter have **[D]ecode ROT-13**, a
toggle that shows the body ROT-13 decoded. It is offered on every board and
for every letter, not only FTN ones, since a spoiler in ROT-13 can be
written anywhere; a post drawn in the ANSI art editor, and a post taken down
or withdrawn, don't offer it. Only ASCII letters turn; escape sequences and
pipe codes stay as they are. It is display-only: the stored body never
changes, nothing about the toggle is stored, and a reply quotes the text as
written. On a board it holds for the post on screen: stepping to the next or
previous post shows that post as written.

**Character sets.** Text is decoded by its `^ACHRS` kludge (FTS-5003). Without
one, the network's default is used, CP437 unless the SysOp sets another.
Outbound text is written in CP437 when every character fits, and in UTF-8
otherwise. The kludge always names the set used. To, From and Subject are cut
on character boundaries to FTS-0001's 35 and 71 bytes, except a real-name
echo's From, which is never cut (above). Inbound text is
sanitized before display like any other external content.

**Planned: the rest of the FTN round** (§16, "Issue #1154", tracker #1461).
These are decided in outline and ship before the feature freeze, one stage at
a time, each live-checked on ReLink. Each stage's PR writes its rules into
this section; until then the text above is what holds.

- **Stage D:**
  - the hub's second part: echomail fan-out, an AreaFix/FileFix server and
    TIC forwarding (#1479), on the link records above;
  - pointlist and nodelist segment publishing (#1480).
- **Stage E:**
  - QWK offline mail for callers (#1389) -- built, see §6.10;
  - QWKnet as a node (#1481) and as a hub (#1482) -- built, see §6.11.

### 6.9 The oneliner wall (issue #1391)

The oneliner wall is a short list of the latest one-line messages callers
leave for whoever calls next. It is local to the node; a Link-wide wall would
need a signed event kind of its own and is a separate, later issue.

- **At logon,** after sign-in, the wall is the lower section of the Recent
  callers frame (§3.3), so a call has one pause. Each section follows its own
  SysOp toggle and is left out when empty; the wall shows at most 10 lines
  and is left out when not one fits. The keys are `[A]dd a line   [Enter]
  Continue`; the logon never asks a question about the wall (§3.5).
- **On the menu,** `[R]ecent callers` draws the same panel, and its bar adds
  `[A]dd a line` and `[O]neliners`, the full wall: newest first, numbered
  rows, paging, Find and help, with `[A]dd a line`, `[D]elete` and `[B]ack`.
  An empty wall says so.
- **A line** is plain, sanitized text of at most 60 display columns, typed at
  one prompt; Esc or a blank answer cancels, and the outcome is carried into
  the redraw. A caller adds at most one line per call and 3 per account per
  day, counting lines added even if since deleted. Who may write is a node-wide minimum level, default 0; a guest writes
  only if the guest account meets it. `[A]dd a line` is hidden below the
  level and refused with its reason at the rate limit.
- **Deleting:** the author deletes their own lines, and a SysOp or a staff
  member who may moderate everything deletes any, after "Delete this line?
  (y/N)". A moderator's deletion is audited. The block list does not hide
  lines (§6.4).
- **Storage:** its own `oneliners` table, newest 200 lines kept. A line shows
  its author's account name, kept as a label so the line outlives the
  account; the Recent callers privacy choice does not apply to a line the
  caller chose to write.
- **The SysOp toggle,** shown, plain or hidden, sits beside Previous callers
  in System settings and governs the logon panel only.

See §16, "Issue #1391 — the oneliner wall".

### 6.10 QWK offline mail (issue #1389)

A caller can take their mail and the boards they follow away as a QWK
packet, read and answer it offline in a QWK reader such as MultiMail, and
upload the reply packet the reader makes. Decided in §16, "Issue #1154",
Decisions 21-23 and 25 and the batch's principle; QWKnet (#1481, #1482)
builds on the same format code.

**Where.** `[Q]WK offline mail` on the top-level message board list (the
unfiltered list `[M]essage boards` opens; not a category's or a Community's
list). Decision 25 named the action `[O]ffline mail`, but that list's `[O]` is
`[O]rder`, so the key is the first letter of "QWK offline mail". The screen
shows the packet and reply file names, what is waiting (unread letters,
unread posts on the boards packets carry), and how many messages earlier
packets carried that are not yet marked read. Its bar: `[D]ownload packet`,
`[U]pload replies`, `[M]ark read` (while anything waits to be marked),
`[C]onferences`, `[R]eport` (after an upload in this call), `[W]eb transfer`
(when the terminal has Zmodem and the node hands out links) and `[B]ack`.
The guest account is turned away: its read state and replies would be every
guest's.

**Transfers** go the way a file area's do (§6.2): Zmodem where the terminal
has it, otherwise a single-use browser link. The link is a grant like a file
area's, naming the account and re-checked when used, for a file in no area: a
packet staged under the file store's staging directory (removed once its
link is long expired, and at startup), or an upload handed straight to the
reply reader and never stored.

**Conferences** (Decision 21). Conference 0 is the caller's mail. Each board
gets a number, per caller, the first time a packet carries it, and keeps it
(`qwk_conferences`, keyed by the board's content id): a reader files messages
by number, and renumbering would mix two boards together. A board is carried
while the caller follows it, unless `[C]onferences` took it out, and a board
they don't follow is carried when `[C]onferences` put it in; follows are never
changed. `[C]onferences` is a tick list of every board the caller may read,
saved with `[S]ave`. Only boards the caller may still read are carried or take
replies.

**A packet** holds the caller's unread letters (oldest first), then each
carried board's unread posts as New scan counts them, oldest arrival first,
up to the SysOp's cap (2,000 messages by default, 50-10,000, **Settings ▸
Limits & retention**) and 16 MiB of text; the rest wait for the next packet.
A cross-post is carried once. A carried board the caller has never visited is
visited when the packet is built, as opening it would be (§6.6): nothing on it
is new before that, so it adds nothing to that packet and its later posts go
in the next. A message's number is its local row id (a post's root row, a
letter's id), which a reply names as its reference.

**Format** (Decision 23). A packet is a ZIP named by the node's QWK BBS ID
(1-8 letters and digits, a SysOp setting that defaults to the node's display
name cut to fit) holding `CONTROL.DAT`, `MESSAGES.DAT` (the Qmail copyright
block, then 128-byte header and text blocks, lines ended by 0xE3), an
`NNN.NDX` per conference and `PERSONAL.NDX` (block numbers as Microsoft binary
singles), `DOOR.ID` (`MIXEDCASE = YES`), QWKE's `TOREADER.EXT` (per-conference
flags: `pP` for mail; `aO` for a board, `R` where the caller can't post, `E`
for a shared or FTN board, `L` otherwise) and a short `HELLO`. Text is CP437;
a character it lacks is written `?`. A To, From or Subject longer than QWK's
25 characters gets a QWKE `To:`/`From:`/`Subject:` line at the top of the text,
followed by a blank line. Posts are carried as a reader with colour off sees
them, with their file references as text lines; a reply's To is the author
of the post it answers.

**Read state** (Decision 22). Building a packet marks nothing read; it
records what the packet carried (`qwk_sent`). The next reply packet that can
be read, or `[M]ark read` (after a yes/no), marks every one of them read -- a
post as opened (`record_post_opened`, so the copies of a cross-post too), a
letter as read -- and empties the list. A download while messages are still
listed asks whether to include them again; without them, the packet carries
only what no packet carried. At most 20,000 messages are listed per caller.

**Reply packets are untrusted input.** The reader (`netbbs.qwk.rep`) bounds
the upload (2 MiB), the `.MSG` inside it (4 MiB, measured as it inflates,
whatever the ZIP claims), its members (32), its messages (200) and each text
(a post's 200,000 bytes). The first block must carry this node's BBS ID. A
damaged block count ends the walk, reported, since nothing after it can be
found again. Text is decoded as CP437 and control characters are dropped. The
conference is the header's ASCII number field, else its binary word. A QWKE
`To:` or `Subject:` line is used only where it continues the header's 25
characters; a `From:` line is never used: a reply is always its uploader's.
A packet that can't be read posts nothing and marks nothing read.

**Each reply** goes the way the same message written online goes, and the
report names what became of it (posted, held for a moderator, sent, or
refused and why):

- In a board's conference: refused for a board the caller may not read (or
  a number they were never given), a private message (a board has none: such
  a message is written as a letter), a blank subject or text; then the
  board's own checks before `[P]ost` -- closed, write level, name and age
  gates, the real-name refusal (#1443), the origin's posting rule. A reference
  to a post the caller can still see makes it a reply; to one gone, it starts
  a thread, and the report says so. It is then created by `create_post`
  (held on a moderated board; the verified name frozen as for any post,
  #1453), marked read for its author, signed onto Link on a Linked board when
  Link is on, and queued for its FTN network.
- In conference 0, a letter, after Mail's own gate: a reply to a letter the
  caller received goes back to its writer -- locally, over Link (checked as
  `[R]eply` checks it), or as netmail -- and a letter from the BBS takes no
  reply. Otherwise the To line names a local account (`SysOp` as the online To
  prompt reads it) or an FTN address; a new letter to another BBS is written
  online.

The same reply uploaded twice posts once: what each upload posted is
remembered by digest for 90 days, up to 2,000 per caller. A QWKE reader's
`TODOOR.EXT` `AREA n D` takes conference *n* out of the caller's packets
(`a`, `p` or `g` puts it back); unknown numbers are ignored.

See §16, "Issue #1154 — the FTN round before the feature freeze".

### 6.11 QWKnet (issues #1481 and #1482)

NetBBS takes part in QWK networks -- DOVE-Net and the other networks
Synchronet systems run -- as a node of a hub, and as a hub other systems
call. Decided in §16, "Issue #1154", Decision 24 (node and hub, conferences
mapped to boards like echoes), with Decision 23's format and the batch's
principle that what arrives is untrusted. It reuses §6.10's packet and
reply-packet code.

**How QWKnet works** (researched 2026-10-23 from Synchronet's wiki pages on
DOVE-Net, QWKnet and the QWK format, and its `qnet-ftp.js`, `qnet-http.js`
and `qwk.ssjs`). A node has an account on its hub's BBS named after the
node's QWK ID and marked as a network account (Synchronet's `Q`
restriction: its reply packets may carry any author name, and it sees only
networked conferences). On each call the node uploads a reply packet named
for the hub (`VERT.REP`, holding `VERT.MSG`, whose first block is the hub's
ID) and downloads the hub's packet (`VERT.QWK`); the hub counts the packet
sent once the transfer completes. Conference 0 is mail; the other
conferences are the hub's sub-board numbers (DOVE-Net's are 2001-2030).
Synchronet carries a message's identity in `HEADERS.DAT`, an INI file in
the packet keyed by each header's byte offset: `Message-ID`,
`In-Reply-To`, `WhenWritten`, `Sender`, and `SenderNetAddr`, the route back
to where the message was written, nearest first. Older systems carry the
same as `@MSGID:`, `@REPLY:`, `@TZ:` and `@VIA:` lines at the top of the
text. A system drops a message whose route names it ("circular path"). A
message written on a node ends with a `---` line and a tagline
(` ■ Synchronet ■ <tagline>`), and its header's last byte is `*`. The
transport is whatever the hub offers: Synchronet nodes call with
`qnet-ftp` (FTP: `STOR vert.rep`, `RETR vert.qwk`) or `qnet-http` (HTTP or
HTTPS against the hub's `qwk.ssjs`, below).

**A network record** (`qwknet_networks`, **Settings ▸ QWKnet networks**)
holds the network's name; this node's QWK ID there (2-8 letters and digits,
starting with a letter: the login at the hub, and how routes name this
node); the hub's QWK ID, host, port and transport, and the password of this
node's account at the hub; the poll interval (240 minutes by default,
15-1,440); the tagline (the node's name when empty); and whether node
accounts may call. A network with no hub ID has no uplink: this node is that
network's top hub. A network is off until enabled. A new record starts as
DOVE-Net through Vertrauen (`VERT`, `vert.synchro.net`, HTTPS).

**Calling a hub.** NetBBS speaks what Synchronet's own nodes speak, and
invents nothing:

- **HTTPS or HTTP**, as `qnet-http` does it, with Basic authentication:
  `POST /qwk.ssjs` with the reply packet as the body (200), `GET /qwk.ssjs`
  for the packet (200, or 204 when nothing waits), then
  `POST /qwk.ssjs?received=<bytes>` once the packet is tossed (204), which is
  what makes the hub count it sent. Redirects are not followed, so the
  password goes only to the host the SysOp named. Plain HTTP sends the
  password readable and is offered only for hubs without TLS.
- **FTP or FTPS**, as `qnet-ftp` does it. Passive mode only: an active-mode
  data connection would have the hub connect to this node, so `PORT` is
  never sent. The data connection goes to the control connection's address,
  never to an address a `PASV` reply names, so a hub can't point this node
  at a third host. FTPS adds `AUTH TLS`, protects the data connection too,
  and checks the certificate against the system's authorities.
- **Not BinkP.** Two NetBBS systems already reach each other over HTTPS, and
  a hub's FTP or HTTP is what every existing node speaks, so a BinkP path
  would be a protocol no other QWKnet system uses.

Every network operation has a timeout, and a downloaded packet stops at
64 MiB.

**The mailer** (`netbbs.qwknet.mailer`) calls each enabled network's hub
when the poll interval has passed, when posts wait to go up and the last
call was at least 15 minutes ago, or at once on **[P]oll now**. A call
uploads first, and moves the upload cursor only once the hub has taken the
reply packet; it tosses the fetched packet before telling the hub it
arrived. A call cut off part way therefore loses nothing, and what comes
twice is a duplicate. A failed call is retried after 1, 2, 4 ... minutes,
never later than the poll interval. Each call's outcome is stored on the
network and listed.

**Conferences map to boards** (`qwknet_conferences`): one conference of one
network per board, by the board's content id. A board is local, Linked, FTN
or QWKnet, never two (§16 "Issue #166", Decision 3, as extended by "Issue
#1154"): mapping refuses a Linked or FTN board, and Link and FTN refuse a
board that carries a conference. The network's **[M]ap conferences** lists
the boards that may carry one; picking a board asks for its number, and an
empty line makes it local again (its posts stay). Polls are not offered on a
QWKnet board, as on an FTN board: QWKnet's vote packets are not carried.

**Tossing** -- a hub's packet on the node, a node's reply packet on the
hub -- goes message by message, each in its own transaction:

- **Untrusted and bounded.** Packets are read by §6.10's strict readers.
  Text is CP437, or UTF-8 where `HEADERS.DAT` says `Utf8`; control
  characters are dropped, and Synchronet's Ctrl-A colour codes with them.
  `HEADERS.DAT` values are cut at 1,024 characters, and its sections and
  fields are bounded. A hub's packet must be the hub's: one that can't be
  read, or whose `CONTROL.DAT` names another BBS ID, is held whole (the
  newest 10 per network). A node's reply packet must carry the hub's ID in
  its first block and is bounded by the node's own limit (4 MiB by default)
  as it arrives; one that can't be read is refused with the reason, and
  nothing in it is used.
- **The route** is the system the message came from (the hub, or the node
  that uploaded it), then its `SenderNetAddr` or `@VIA:`, read as QWK IDs up
  to 32 hops. A message whose route names this node is dropped as a loop.
- **Duplicates** are dropped by Message-ID (`HEADERS.DAT` or `@MSGID:`),
  else by a digest of the conference, author, subject, date and text, per
  network (`qwknet_seen`, kept a year). A message carrying a Message-ID this
  node gave out is its own post coming back.
- **Posting** is on the board carrying the conference, through
  `create_labelled_post`: held on a moderated board, and dated by the
  message (`WhenWritten` with its offset, else the header's date in the
  node's time zone; never later than now). The author is labelled
  `Name (ORIGIN)`: the name the message carries and the QWK ID at the far end
  of its route, where it was written. It never contains `@`, which names a
  Link address. A node's messages carry the names of its own callers -- a
  network node speaks for them, as on Synchronet -- and the node's ID shows
  where they came from.
- **Threads** follow `In-Reply-To`/`@REPLY:` to the post with that Message-ID
  on the same board, else the header's reference: the hub's message number
  in a hub's packet, this node's in a node's reply packet.
- **An unmapped conference** is held per conference (`qwknet_held`, the
  newest 2,000 messages per network, for 30 days), like FTN's unknown
  echoes. Once a board carries it, **[R]e-toss** under **[H]eld** tosses its
  messages through the same checks; **[D]elete** drops them.
- **Mail.** A hub's conference 0 is mail for this node: to `SYSOP` or this
  node's QWK ID it goes to the SysOp, to an account's name to that account
  (as external mail, labelled like a post), and anything else -- routed
  `NETMAIL`, a name with no account, a full mailbox -- is held. Conference 0
  in a node's reply packet (Synchronet control messages, routed netmail) is
  not carried. Votes (status `V`) are skipped.

**Out.** Every post that becomes visible on a mapped board of an enabled
network -- written or approved here, or tossed in -- is numbered in
`qwknet_outbound` by `queue_post_if_qwknet`, which is called beside the FTN
scanner wherever a post becomes visible: once per post, and never for an
edit. Each destination keeps a cursor into those numbers, the hub's
(`uplink_seq`) and each node's (`sent_seq`). A destination never gets a
post that came from it (what the hub sent never goes back up, what a node
sent never goes back to it), nor one whose route names it.

- **To the hub**, in the reply packet, up to 1,000 messages a call: the
  author's name (a local username, or the name a node's message carries),
  the conference, the hub's number of the message it answers as the
  reference, and `HEADERS.DAT` with `Message-ID`, `In-Reply-To`, `Sender`,
  `To`, `Subject` and, for a node's message passed on, `SenderNetAddr`. A
  post written here gets the Message-ID `<content-id.row@OURID>`, a `---`
  line, ` ■ NetBBS ■ <tagline>` and the header's network tagline flag. No
  body kludges are written: `HEADERS.DAT` carries them, as Synchronet 3.15
  and later read it.
- **To a node**, in its packet `<OURID>.QWK`: the conferences it carries
  (QWKE flags `aOE`), up to its packet size (2,000 messages by default,
  50-10,000) and 16 MiB of text, each message numbered by its local row id.
  The packet built is kept and served again until the node confirms it, and
  the node's cursor moves only then.

**The hub** (#1482). Node accounts (`qwknet_nodes`, the network's
**[N]odes**) log in with their QWK ID -- unique across networks, since the
login is how a call finds its network -- and a password kept only as an
Argon2id hash. Each has its conferences (all of the network's when
created), its packet size and its reply-packet limit, and starts from the
newest queued post rather than the backlog. Nodes call `/qwk.ssjs` on the
web listener, which speaks exactly what Synchronet's `qwk.ssjs` speaks: a
Synchronet node uses its stock call-out command
`?qnet-http -s %s <host> <password>`, and a NetBBS node its HTTPS
transport. Every request is a login; there is no anonymous access, and only
an enabled account on an enabled network that serves nodes gets in. A
source address that fails 10 logins in 15 minutes is refused for the rest of
that window (429), at most 4 requests run at once (503 past that), and the
password is checked on the shared Argon2 pool. A node's reply packet is
tossed at once and answered 200 with what it came to, and the account shows
its last fetch, upload and outcome.

**No FTP server.** Serving packets over FTP would mean embedding an FTP
server -- a second listener, with `PORT` and `PASV` data connections to get
right -- for what one route on the existing web listener does. Synchronet
nodes have called hubs over HTTP since `qnet-http` (2019). Basic
authentication sends the password readable, so a hub's web listener belongs
behind TLS, as it already does for callers' logins; the SysOp Handbook says
so where node accounts are set up.

**Not carried:** routed QWKnet netmail (`NETMAIL` with a `name@ID` line) and
mail between a hub and its nodes; Synchronet's control messages (`ADD`,
`DROP` and the rest); vote and poll packets (`VOTING.DAT`); file requests
and attachments; and messages passed between two networks.

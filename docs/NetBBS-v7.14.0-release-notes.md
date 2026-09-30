# NetBBS v7.14.0

This release covers everything merged since v7.13.0. It has five parts.

- **E-mail.** The 2026-09-28 mail audit (#803) is done. Mail between nodes
  now works as a product: callers can reply to Link mail, see where a letter
  is, and hear when it bounces, and trust probation no longer swallows it
  silently. Local mail gains a mailbox screen, drafts, a better editor,
  several recipients, forwarding, read receipts, blocking, search and file
  links. The SysOp gets a mail level and tools for refused Link mail.
- **A first-time SysOp and first-time callers.** Fixes from the "Nib & Quill"
  field test (#831): a flatter main menu, lists that stay in the SysOp's
  order, Back that goes one level up, a New scan that walks you through what
  is new, help for a first call, and honest signup and approval messages.
  SysOps can give helpers **staff permissions** instead of a second level-255
  account, and members see a Staff list.
- **Classic terminals.** NetBBS now talks to CP437 terminals such as SyncTERM
  in CP437, and to plain-ASCII terminals in ASCII (#929, steps 1 and 2; art
  on menus is still to come). A field test with SyncTERM fixed its keys,
  layout, SSH login and Zmodem transfers (#963, #964).
- **Chat.** An alias always shows the account behind it, linked speakers read
  `<user@Node>`, and look-alike node names are caught.
- **Doors.** A BBSLink connector, verified against the live service, and
  the DoorParty template fixed after live use.

**It migrates: the node database goes from schema 87 to 106.** Other versions
stay as they were: `NETBBS_PROTOCOL_VERSION` is 1,
`REALTIME_PROTOCOL_VERSION` is 4, `DOOR_API_VERSION` is 4, Voidrunner careers
are save schema 2 with tactical ruleset 3, and War Dialer worlds are world
schema 11. Link mail gains new bounce reasons and fields inside the sealed
letter, which older nodes accept; see *Mail over Link*. No key in
`netbbs.toml` changed. Rolling back needs a restore; see *Upgrade and
rollback*.

**Callers will notice on the first call after the upgrade:**

- Boards and file areas are listed in the SysOp's order, not by activity,
  unless the caller chose an order before.
- A chat alias holding a character that is now reserved, such as
  `Lee[ops]`, is no longer shown: the caller appears under their username
  until they choose a new alias. See *Chat*.
- A Telnet client that names no terminal type NetBBS knows gets plain ASCII,
  and a terminal not recognised as a modern UTF-8 one is laid out one column
  narrower. See *Terminals*.
- Read receipts are on for everyone, and letters read before the upgrade
  show as read to their senders.
- `/msg` and `/private` respect the Direct messages opt-out.

## First-time callers

These changes come from a field test with a first-time SysOp and first-time
callers (#831).

### A flatter main menu (#856, #857, #859, #879)

- **`[M]essage boards`, `[C]hat` and `[F]iles` list every board, channel and
  file area on the node**, whichever Community it belongs to. A board outside
  every Community is simply a board. `[U]ncategorized` and `[J]ump to...` are
  gone.
- **`[G]ames` shows only while you can see a door**, and **`C[o]mmunities`
  only while you can see a Community**.
- **Find moves to `[/]`**, because F is now Files. Its description names what
  it searches: posts, files and chat, plus your own mail where mail is open to
  you.
- **A Community has a page.** It shows the description and how many boards,
  channels, file areas and door games the Community holds. It offers only the
  kinds the Community actually has. `[B]ack` from it returns to the Communities
  list, on the Community you just left.
- **Communities follow the SysOp's order**, not alphabetical order.
- **One number per row.** `02. (#1) Fountain Pens` is now `02. Fountain Pens`,
  and tables have lost their `#` column. `[G]oto #` is gone. When a row key
  such as New scan's `[M]ark read` has nothing highlighted, it asks
  `Which one (01-NN):` and takes the row's number on the page you are looking
  at.

### Lists stay in the SysOp's order (#864)

- **Boards and file areas no longer re-sort by activity on every visit**, so
  the "03" you remember stays the same board. The default is now the SysOp's
  order. **`[O]rder`** gains **`[S]ysOp's order`**. Activity, alphabetical,
  recent and volume are still available, and an order you chose before is
  kept. Chat channels stay alphabetical.

### Back and New scan (#869)

- **`[B]ack` goes one level up.** Back from a board or file area returns to the
  list you picked it from, with the cursor on its row. Back from a category's
  list returns to the list above it.
- **New scan walks you through what is new.** Back from a board, channel or
  area you opened from New scan returns to the scan. The cursor then sits on
  the next row with something new, and a line names it: "Next with something
  new: Inks. Enter opens it." When nothing is left, it says "Nothing else is
  new." A board or area you have never visited reads "not yet visited, 3
  posts" and counts as new.
- **`[R]eplies` in New scan** lists replies to your posts, one per row.
  Picking one opens its board with the cursor on that reply.
- **A jump no longer renumbers the board.** If a New scan or Find result is on
  the newest page, that page opens as a normal visit shows it, read posts
  included, with the cursor on the result. A page that starts at the first
  unread post is used only when there are more unread posts than one page
  holds. File areas work the same way.

### Help for a first call (#871, #933, #950)

- **`[?] Help` on the main menu**, also on Ctrl-H there. It explains one-key
  menus, list numbers, Back and New scan. It names the node's SysOps and the
  User Handbook's address.
- **A single digit and Enter selects a row**, so `3` and Enter picks row 03.
  The list hint now reads "or type a number to select".
- **Typing a whole word at a one-key prompt no longer runs extra commands.**
  After a main-menu key or a yes/no answer, the rest of the word and its Enter
  are dropped. The word counts as ended once 0.6 seconds pass between two
  letters, or when you type a digit, an arrow or other non-letter. `Y`, Enter,
  `N` is still read as two answers.
- **Clicking works in the browser terminal.** Clicking a menu entry sends its
  bracketed key, and clicking a numbered row sends its number. Any other click
  shows the hint "Use your keyboard: press the letter in [brackets]" for five
  seconds. Door games and text selection are not affected.
- **`sysop` works as an E-mail address.** It reaches the node's first usable
  SysOp account, unless an account is actually named `sysop`.
- **Login notices are no longer wiped by the screen clear.** The Welcome line,
  the answer to a login question, the drain warning, and the pending chat
  invitation count now appear above the first main-menu prompt. Each appears
  once: Ctrl-L doesn't bring it back. The chat invitation line reads "You
  have N pending chat channel invitation(s) -- [I]nvitations to see them."

### Signing up on a node that approves accounts (#850, #855, #870)

- **A pending account is told the truth.** Logging in with the right password
  now says "Your account 'lena_h' is waiting for the SysOp's approval…" and
  ends the connection. It used to say "Login failed". A wrong password stays a
  plain failure. On SSH the notice arrives as the login banner.
- **The message after signup is written for a newcomer.** It says that the
  SysOp checks new accounts by hand, that until then you can't log in or look
  around, and to call back later.
- **Your username is checked before the password prompts.** If a name is
  taken, too long, uses non-ASCII letters, is reserved or looks like the
  SysOp's name, you hear it at once. SSH asks again in place, up to three
  times, so you don't have to reconnect.
- **Some names can't be registered by callers:** `sysop`, `cosysop`, `admin`,
  `administrator`, `root`, `moderator`, `mod`, `staff`, `support`, `system`,
  `operator`, `postmaster`, `guest`, `netbbs`, anything containing `sysop`,
  and names that look like a SysOp's. Look-alikes are matched ignoring case,
  `_ - .`, and pairs such as 0/o, 1/l/i, 5/s, rn/m and vv/w.
- **The SysOp may ask one question at signup** ("The SysOp asks: …"). Enter
  skips it. An answer is cut to 300 characters.
- **If everyone who approves accounts is away**, the pending message says who
  is expected back first, and gives their note.

### Staff list and Moderation (#866, #870)

- **`S[t]aff list` on the main menu** shows who runs the node, for every member
  except the guest account. It lists SysOps first, then staff, then
  moderators, with what each one looks after and the date they were last on.
  It also shows any away notice. The Previous callers privacy choice hides
  nobody here.
- **`Moder[a]tion (n)`** appears for anyone who approves posts or uploads
  somewhere. It opens one queue covering everything they moderate, and the
  count shows how much is waiting.

### Files (#886, #913)

- **A browser upload reports back.** The terminal that made the link says
  `Uploaded 'x' (15 B) to [Area].` In a moderated area it adds that the file
  waits for approval. An area's first upload now appears after Ctrl-L. It
  used to stay hidden behind "This file area has no files yet". A printed
  link on SSH or Telnet adds "Once it has uploaded, press Ctrl-L here to see
  it."
- **A link upload from a browser tab gets a short page, not raw JSON.** The
  page says the file arrived and that you can close the tab.
- **A failed Zmodem transfer keeps you on the file list**, and says "Press [W]
  for a browser download link instead" when the node can make links. A
  successful transfer returns to the menu, as before.
- **Wording:** "Open this in a browser to download 'x':", and "Your browser is
  starting the download." when the page takes the download itself.
- **Long filenames are cut in the middle** (`copperplate-m...-week1.png`), so
  the extension stays visible. The uploader column is only as wide as the
  widest uploader name on the page.
- **A post can point at files in this node's file areas.** Use `[A]ttach file`
  and `[R]emove file` on the review screen of a new post, reply, art post or
  edit. A post can point at up to five files. Readers see them under the
  byline and download one with `[G]et file`, by Zmodem or a browser link. A
  reader who can't open the file's area sees "A file in a file area you can't
  open", with no names. A deleted file shows as "no longer available". The
  review screen warns you when the area is stricter than the board. On a
  Linked board it also says that other BBSes get text lines instead of a
  download, and those lines count toward the length limit. A saved post
  draft keeps its text only, not its files.

### Door games too big for your terminal (#957)

- **A door that needs a bigger terminal now tells you so.** You see, for
  example, "DoorParty needs a terminal of at least 80x25; yours is 80x24.
  Enlarge your window and try again." It used to report a failed start. The
  node no longer logs this as a warning, and the SysOp's Check setup screen
  still lists the size requirement.

### SSH keys (#884)

- **Adding an SSH key asks for the key first, then its label.** The comment of
  an OpenSSH line (`kai@laptop`) is offered as the label. A label that is
  itself a key is refused.

## Mail

The 2026-09-28 mail audit (#803) found that local mail worked but looked like
NetBBS before the board overhauls, and that **mail between nodes did not work
as a product**. A caller could not reply to it, Sent listed every remote
letter as "to (deleted account)", nothing showed whether it arrived, and
between newly linked nodes the recipient's trust probation turned it away
while the sender was told "Message sent." Every item in the audit is now done.

### Link mail you can use (#804, #805, #806, #807, #808, #874)

- **Mail flows once the SysOp establishes the other node.** Mail from a
  user on probation at an established node is now delivered. A node still on
  probation, or a quarantined or blocked node or user, is still refused. The
  refusal is never silent any more: the sender gets a bounce with a reason.
- **The To prompt checks a remote address as you type it** and asks again in
  place: a malformed address, a BBS this one is not linked with, a name
  several linked BBSes go by, or a BBS still on probation ("Farpoint is not
  linked yet; mail opens once the SysOp establishes it."). Before, all of
  these surfaced only after the letter was written.
- **Names work as shown.** `OldNib@Q`, typed as the From line shows it, now
  works; capitals used to be refused. A node name containing `@` is shown in
  quotes (`bob@"Ça @ Cœur"`), and what you read after the `@` can be typed
  back. A node name no longer clashes with fingerprint prefixes shorter than
  6 characters, so a one-letter node name is no longer ambiguous. Error
  messages say what to type, not `expected 1-32 chars from [a-z0-9_.-]`.
- **`[R]eply` works on mail from another BBS,** with `Re:` and the quote, to
  the sender's stable address. If the node has since gone on probation, been
  closed, or is no longer linked, Reply says so before you write anything.
- **Sent shows where remote mail stands.** Its Delivery column reads
  `pending`, `with relay`, `delivered`, `bounced` or `expired`, and the letter
  has a `Delivery:` line with the reason in plain words, for example "Bounced:
  there is no user by that name on that BBS."
- **You are told when Link mail bounces or expires,** once, at your next
  main menu, even if you were offline when it happened. Up to 10 letters are
  named; more are counted. Opening the letter in Sent also counts as told.
- **Mail left at a relay times out.** A letter handed to a relay shows `with
  relay` and expires after **14 days** with no answer, with a notice that it
  "may not have arrived". A late acceptance or bounce still wins. Mail that
  was already pending at upgrade is not timed out; it keeps waiting as before.
- **Received Link mail is dated when it was written,** from the sender's
  signed time, so a letter that took days to arrive says so. A date more than
  five minutes in the future, or before 2000, is replaced by the arrival
  time. The mailbox still lists by arrival, so a late letter lands on top.

### The mailbox is a screen (#810, #809, #828, #921)

- **`[E]-mail` opens straight on the Inbox,** a table like the board list:
  `#`, `new`, From, Subject, Date. The four-option mail menu is gone. The
  header counts unread mail and how full the Inbox is (`3 unread messages ·
  120 of 500`). `[S]ent` and `[K]ept` are the other folders, and `[B]ack` from
  them returns to the Inbox. Below 60 columns a row becomes one line of prose.
- **`[O]rder`** cycles newest first, unread first and by conversation (the
  correspondent plus the subject without `Re:`/`Fwd:`), and is remembered.
  **`[F]ind`** filters by name or subject.
- **Mark unread:** `[U]nread` in a letter, `[U]` on the list toggles.
- **Many at once:** `[M]ark` or Space marks letters; **`De[l]ete`** deletes the
  marked ones (or the highlighted one) after one yes/no; **`Delete [r]ead`**
  empties the Inbox of read mail.
- **Kept folder.** `K[e]ep` moves letters to Kept, `Mov[e] to Inbox` moves
  them back. Kept holds **100 letters** of its own, outside the Inbox's
  500-letter cap, and the 500 cap never removes a kept letter. Keeping more
  than fit is refused, and keeping several marked letters is all or none.
- **A letter keeps its lines.** The reader no longer merges the writer's lines
  into paragraphs, so greetings, lists and signatures stay as written. Only a
  line wider than the screen wraps.
- **Color in mail.** Pipe codes and pasted color show, through the same filter
  as posts on a board that allows color: color only, never cursor moves or
  other escape sequences, including in mail from other nodes. There is no
  SysOp switch. **Profile → Pos[t] colors** now covers posts and mail; with
  it off, mail shows as plain text.
- **Received mail's view has a `To:` line.** A letter from a sender whose Link
  identity changed shows `!` before the name on the list.

### Writing a letter (#812, #813, #814, #825, #822, #827, #826, #920, #830)

- **Composing has its own screen,** titled "New message", "Reply" and so on,
  with To and Subject asked there. The To prompt says in plain words what to
  type, and Esc or an empty line cancels. The fullscreen editor shows To and
  Subject above the text. Review pages a long letter and keeps To and Subject
  on every page, and shows the account's own spelling (`Alice`, not `ALICE`).
- **Limits are checked where you type,** in characters, never bytes. An
  over-long subject is refused on Enter ("That subject is 29 characters too
  long"), and the prompt reopens on it. An empty subject asks again instead
  of cancelling the letter. Review catches a signature that pushes the body
  over and names the key that fixes it.
- **Drafts per letter.** Each new letter, reply, forward and resend has its own
  draft, kept with its To and Subject, in both editors. The mailbox shows
  "You have an unfinished letter to bob: Lunch?" and `[D]raft` to resume or
  discard it. A kept draft no longer replaces a later reply's quote. A
  disconnect on the review screen still loses the letter, as it does a post.
- **Several people at once.** Separate addresses with commas, up to **20**,
  local and remote mixed. Each is checked as typed. Everyone sees the whole
  To list; there is no blind copy. A letter goes to all or none: a refused
  recipient is named on review and `[T]o` drops or fixes them. Received
  copies get **`Reply [a]ll`**, and Sent shows one row per letter.
- **Finding a recipient.** Tab at To completes a member's name, a recent
  correspondent, or after `@` a linked BBS. `?` and Enter (or `name@?`) opens
  a "Write to" list: recent correspondents, members, then linked BBSes.
  People you cannot write to are listed dimmed with the reason: "doesn't
  accept your mail", "account disabled", "awaiting approval", "not linked
  yet". Once resolved, a remote address is held by the node's fingerprint, so
  a resumed draft still reaches the BBS you chose.
- **`[F]orward`** from the Inbox and Sent, local or Link either way. The
  subject gets `Fwd:`, and the letter is passed on whole under a
  `Forwarded message` header, not quoted. Your note and signature go above it.
- **Sent has `[R]eply` and `Re[s]end`.** Reply writes to the recipient again,
  quoting your own letter. Re[s]end appears only on Link mail that bounced or
  expired, and sends the same letter again. After a resend the old letter
  reads `resent`, with a `Resent:` line and `Re[s]end again`. After a Reply,
  Resend or Forward from Sent is sent, you land back on the Sent list.
- **Pointing at a file.** Review's `[A]ttach file` picks up to **5** files
  already in file areas you can read. The recipient gets `[G]et file` only
  if they may read that area; a file since removed reads "no longer
  available". Every local recipient must be able to open every file, or Send
  names who cannot. A recipient on another BBS gets a text line per file, not
  a download. Forward carries the files; Reply and Resend do not.

### The editors, for mail and posts alike (#812, #814, #815, #837, #848, #903)

- **Ctrl-U clears the line** at every single-line prompt, on every transport.
- **The line editor writes paragraphs.** A blank line is a paragraph break; a
  second blank line in a row, or `/done`, finishes. `/insert N` now stays in
  place until `/end`, so answering between quoted lines takes one command per
  answer. `/delete N-M` deletes a range, and `/unquote` removes a reply's
  quote and its "X wrote:" line in one go. `/exit` keeps a draft, and a
  dropped connection keeps the text. The editor mentions the fullscreen
  editor.
- **Fullscreen editor keys:** Ctrl+K cuts a line (again adds the next),
  Ctrl+Y pastes, Ctrl+W or Alt+Backspace deletes a word, Ctrl+R rewraps a
  quoted paragraph to at most 72 columns, and Ctrl+E erases all the text after
  a yes (Ctrl+Y brings it back). The status line counts `used/limit` in
  characters. Paste is Ctrl+Y, not nano's Ctrl+U, because Ctrl-U clears a line
  everywhere else.
- **Profile → Fullscreen editor (all writing)** is the new name of "Fullscreen
  editor for posts/bio". It always governed mail too.
- **Board posts keep their authors' lines,** as mail now does. Lists and
  sign-offs no longer run together. This applies to posts written before the
  upgrade too, which now show their lines as typed.
- **"X wrote:" stays on its own line,** so a reply that trims the quote no
  longer credits its first words to the quoted author.

### Hearing about mail (#823, #917, #944, #824, #918)

- **At login:** "3 new since your last call, 7 unread in all -- [E]-mail to
  read them." On a first call it gives the unread count alone. New scan heads
  its summary with the same line and an `[E]-mail` key.
- **Mid-session:** "New mail from bob: Lunch?" appears at once in chat and the
  SysOp's Monitor, and elsewhere above the next screen, never inside a door
  or editor. An idle main menu or Inbox redraws with the new count. Mail is
  checked every 5 seconds, and at once for local mail.
- **Mail news is green,** a fixed color a SysOp's accent cannot change. Near
  full and removed-mail notices stay amber.
- **`/msg` lines sent while you were outside chat** are no longer wiped by the
  main menu's redraw; they show above its prompt.
- **Find searches your own mail.** `[/] Find` ("Search mail, posts, files,
  chat") lists up to 20 of your letters first, then posts, files and chat.
  It matches subject, body and the From/To names, whole words, ignoring case
  and accents, in your Inbox, Kept and Sent only. A result opens in the mailbox's
  own view.

### Mail from where you meet people (#821, #953)

- **Directory:** a member's card is now a screen with `[M]ail`.
- **Who's online:** `[E]-mail`, for local callers and callers on linked BBSes.
  A caller who turned off direct messages can still be mailed.
- **Previous callers:** `[M]ail a caller` asks for the row number. A row
  whose name is hidden is refused.
- **Board reader:** `[M]ail author` sends a private reply with the post's
  `Re:` subject and quote, to anyone who can read the post, including authors
  on other BBSes.
- Each opens the compose screen with To filled in and returns you where you
  were. None is offered to someone on this BBS who has blocked you.

### Blocking people (#817, #925, #948, #953)

- **Block from a letter** (`Bloc[k] sender`), from **Who's online**
  (`Bloc[k]`), or in **Profile → Bl[o]cked people**, which lists your blocks
  and lets you `[A]dd` someone by name, here or `name@OtherBBS`, or unblock
  them. A block on someone here follows the account through a rename; a
  block on someone elsewhere is on their stable address, not the node's name.
- **A block stops mail, live messages and invitations:** `/msg`,
  `/private`, `/dm`, Who's online messages and `/invite`. It does not hide
  their lines in a chat channel, and it does not cover MRC.
- **The blocked person is told.** On this BBS: "bob does not accept mail from
  you." or "... messages from you." Mail from another BBS bounces with "the
  recipient does not accept mail from you". A live message from another BBS
  is dropped without an answer. Mail already in your Inbox stays.
- **The SysOp and system notices cannot be blocked.**
- **Direct messages opt-out covers `/msg` and `/private` now,** as it
  already did `/dm` and Who's online. Its Profile label is now **Direct
  messages** (was "Direct messages (Who's online)"). A caller who had opted
  out stops getting `/msg` lines on upgrade.

### Read receipts (#829, #922)

- **On by default, for every account, on upgrade.** A sender sees in Sent
  when a local recipient first opened a letter: `Read: <time>`, `Read: not
  yet`, or, for a recipient who does not share receipts, `Read: not shown`.
  The Sent list's column is headed **Status** once it shows receipts (`read`,
  `not read`, `some read`, `no receipt`). Letters read before the upgrade show
  as read, at the time they were read.
- **It works both ways.** **Profile → E[x]change read receipts** off: no one
  sees when you read their mail, and you no longer see theirs. A receipt shows
  only if both of you shared receipts at the first reading and both share
  now, so switching on for a moment reveals nothing. A letter deleted unread
  shows as `not read`.
- **Letters to several people** list who has read, who has not, and who does
  not share receipts. SysOp mail to all callers shows a count, never names.
- Link mail and system mail have no receipts.

### Mail from the system, and your mailbox's limits (#819, #818)

- **Moderation rejections come from System,** not from the moderator's own
  account. A system letter has no `[R]eply`, and it says "A notice from
  <BBS> itself." The body still names the moderator. An account named
  "System" cannot pass itself off as the BBS.
- **You can see how full your Inbox is.** From 450 letters the Inbox warns
  you. At 500, each new letter removes a read one, system notices first; unread and kept
  mail are never removed. When that happens, your next main menu says how many
  were removed, once.
- **Deleting an account no longer deletes the other person's copy.** Sent
  shows `bob (deleted account)` where it used to lose the letter.

### Mail for SysOps

- **Mail level: Settings → Limits & retention → `[M]ail level` (#816).** The
  lowest level that may open E-mail, to read and to send, local and Link.
  Default 0, everyone, so nothing changes on upgrade. Below it the main menu
  has no `[E]-mail` and no mail count. Mail to an account below the level
  still arrives and waits. Stored as `mail_min_level` in the node's database
  settings; no `netbbs.toml` key.
- **The guest account never has mail (#816),** whatever its level: every
  guest shares its mailbox. Local mail to it is refused, and Link mail bounces.
  A guest account that already has mail keeps it, unreadable by guests.
  **MANUAL, if you want it:** to read or clear it, turn guest login off and
  sign in as the account.
- **Disabled accounts and signups awaiting approval take no mail (#818).**
  Local senders are told which. Link mail bounces with "that account is not
  taking mail at the moment", not saying which. Mail already in a disabled
  account stays.
- **Operations → `[M]ail` (#820, #827):**
  - **`[M]ailboxes`** lists every account with mail, fullest first, or
    `[O]rder` by name: letters, unread, read, Kept, system notices and the
    share of the 500 cap, which counts the Inbox only. The overview shows the
    fullest box and how many Inboxes hold 450 or more. A full Kept (100) is
    flagged.
  - **`[R]efused Link mail`** lists Link mail this node refused, by trust
    policy or on delivery (no such user, mailbox full, blocked by its
    recipient, disabled account, guest, unreadable): sender, reason in plain
    words, how it came, and tries. Opening one shows the trust state of its
    node and sender, with keys to their trust screens. At most 500 are kept,
    and at most 50 from any one node. Refusals before the upgrade were not
    recorded.
  - **`[W]rite to all callers`** sends from your own account, signed; callers
    can reply to you. **`[N]otice to all callers`** sends from System, with
    no reply, and reaches you too. Both go to every account that takes mail,
    except the guest and disabled and pending accounts; accounts below the
    mail level get it and it waits for them. Both can point at files. A full mailbox, or a caller who cannot open
    an attached file, is skipped and named. Sending the same letter twice
    reaches nobody twice.
  - **None of these screens shows a subject or a body.** Link mail is readable
    by whoever holds the node's database, and NetBBS offers no screen for it.
- **Trust actions on the peer screen (#820).** Link status → `[P]eers` → a
  node now has `[E]stablish`, `Bloc[k]`, `[C]lear override` and `[T]rust
  details`. Establish and Block set all three dimensions through the
  existing override editor, with its reason and audit trail. The override
  editor's Dimension picker gains **All three**, and Clear override offers
  "All N overrides".
- **Establishing a node wakes its held mail (#804).** Mail your own node held
  back for a peer on probation is sent on the next sync pass, not after up to
  6 hours of back-off. Mail your own policy held until it gave up now expires
  with a reason, instead of staying pending forever.
- **Relay mailbox retention: 30 days (#891).** A relay drops a letter or
  acknowledgement its recipient node has not collected within 30 days of
  deposit, and logs a warning naming the recipient node and the count. This
  runs whether or not the node still serves as a relay. It is a constant,
  not a setting: it must stay above the sender's 14-day relay timeout.
  **Link status → Relays** states the retention and lists, per recipient
  node, how many items it holds (of 50) and the age of the oldest.
- **Find's mail index (#824).** The search index check and rebuild
  (`python -m netbbs.search`, Operations → Search indexes) now cover mail,
  and report counts only.
- **Account deletion (#818)** removes the deleted account's side of its mail,
  keeping the other person's copy. Its outbound Link mail still delivers.
- **Upgrade: thirteen mail migrations,** schema steps 93-103, 105 and 106 (104
  is board file links):
  - 93 (#806): `link_delivery_reason` and `link_delivery_notice_pending` on
    `mail_messages`. Mail that bounced before the upgrade has no reason and
    its sender is not told.
  - 94 (#819): `from_system`, 0 for existing mail. Rejection mail already sent
    stays from the moderator.
  - 95 (#820): `link_mail_refusals`, empty.
  - 96 (#817): `mail_blocks`, empty.
  - 97 (#818): **a rebuild of `mail_messages`** (`recipient_user_id` becomes
    ON DELETE SET NULL; new `recipient_label`), and the table
    `mail_eviction_notices`. It removes rows nobody could see any more: mail
    received over Link, or from an account already deleted, that its
    recipient had already deleted.
  - 98 (#874): `link_relay_handoff_at`, empty.
  - 99 (#824): `mail_search`, a full-text index of every letter's subject and
    body, **filled at upgrade from all stored mail**; the time grows with the
    mail stored.
  - 100 (#828): `kept_at`, empty.
  - 101 (#827): `mail_group_id` and `mail_group_to`, empty.
  - 102 (#829) and 105 (#922): `first_read_at`, copied from `read_at`, and
    `first_read_shared`, 1 for every letter already read. So letters read
    before the upgrade show as read receipts.
  - 103 (#830): `mail_file_refs`, empty.
  - 106 (#919): `resent_at`, empty.

### Mail over Link

No protocol version changes. Everything below is tolerated by older nodes.

- **Five new `link_message_bounced` reasons:** `undecryptable` and
  `malformed` (#808), `no_mailbox` (#816, the guest account),
  `blocked_by_recipient` (#817) and `recipient_unavailable` (#818). A node on
  7.13.0 or older accepts them, since only building a bounce checks the
  reason, and shows a plain "bounced". A decryption failure used to bounce as
  `unknown_recipient`; a letter with a bad body used to be lost without a
  word, and now bounces `malformed`.
- **Receiving is stricter (#808).** A sender name outside the username
  grammar, a subject or body missing, blank, not text or over the limits (200
  bytes, 20,000 bytes) bounces `malformed`. Only accounts older than the
  username rules could send such a name.
- **Trust (#804).** This node delivers mail from a probationary user of an
  established node; a 7.13.0 recipient still refuses it. Relay pickup now
  applies the same trust rule as a direct push, and answers a refused sender
  with `blocked_sender`. A bounce may now go back to a node on probation. A
  recipient's policy 403, which older and newer nodes send alike, is recorded
  here as a bounce with its reason instead of retried until it dead-letters.
- **Letters to several people (#827).** The To list and a group id travel
  inside the sealed plaintext (`"to"`, `"group"`), not as signed fields. A
  7.13.0 recipient ignores them and shows a letter to one person. A malformed
  list is dropped, not bounced.
- **File links (#830)** go over Link as a plain-text line per file at the end
  of the body.
- **No wire change** for the relay timeout (#874), dates (#808, which reads
  the existing signed `created_at`), quoted node names (#807) or blocking of
  Link direct messages (#925), which are dropped with no reply.

## Chat

### For callers

### An alias always shows the account behind it (#865, #916)

A field test showed an alias passing for the SysOp: `/nick` refused only an
exact copy of another username, so "InkWeII", "Ink Well" and "InkWell[sysop]"
were all accepted.

- **The username now stands beside every alias**, as `Quill (lena_h)`: in the
  live stream, join/leave and `/me` lines, `/who`, `/whois`, `/names`, the chat
  status bar and Link direct messages. The `/nick` notice reads
  `*** alice is now known as Quill (alice)`. The old `~alias~` marker and the
  short-lived `alias|username` form are gone. The alias is drawn in its own
  color (your own in your self color) and ` (username)` is muted.
- **Characters an alias may not contain:** `( ) | [ ] < > * ~`, plus anything
  that folds to one of them (a fullwidth `（`, a superscript parenthesis) and
  any other opening or closing bracket, such as `{` or `❨`.
- **Names an alias may not read as:** another account's username, or, unless
  you are a SysOp, a staff title (anything containing "sysop", and the
  reserved titles). "Reads as" ignores case, spacing, punctuation, accents and
  Cyrillic or Greek look-alike letters.
- **Display names** get the same look-alike check, but only against SysOp
  names and staff titles. Two callers can both be "Anna".
- **Existing aliases:** an alias that is still allowed by the character rule
  stays, even one that now reads like a username; the username beside it keeps
  it honest. An alias that holds a now-reserved character (for example
  `Ann (bob)` or `Lee[ops]`) is **no longer shown**; its owner sets a new one
  with `/nick`.
- **Not rewritten:** `/nick` notices already in scrollback keep their old text.
  Linked nodes on older versions still send their own label format.

### Direct-chat invitations reach busy callers (#865)

- **A caller on another screen is told.** An invitation to someone who is not
  at the main menu now arrives as a one-line notice: "alice invites you to a
  direct chat. Go back to the main menu within a minute to answer."
- **Not inside a door or a file transfer**, which own the screen. The
  inviter's waiting screen says the invitation opens when the other caller
  reaches the main menu.
- **No blank screen after `/close`.** The main menu redraws after an
  invitation is handled, with a decline shown above its prompt. The Who screen
  no longer asks "Press any key to continue..." after a direct chat.

### Linked speakers read `<user@Node>` (#905, #906)

- **The DNS name is gone from every chat line.** `<Phase4Ops@OutBound ·
  outbound.netbbs.org>` is now `<Phase4Ops@OutBound>`, in live chat,
  scrollback and Link private messages.
- **A node is qualified only when its name is shared** with another node this
  BBS knows, or with this BBS itself: `Name · dns.example`, or
  `Name · abc123` (the start of its fingerprint) when it has no DNS name. That
  form can be typed back, for example in `/private user@Name · abc123`.
- **Look-alike names count as shared.** "0utBound", "Out Bound" and
  "OutBоund" (Cyrillic о) are now treated as the same name as "OutBound" for
  this, and for the node map, the Who listing and the board-origin transfer
  picker. A typed address still has to match exactly.
- **The label is colored in parts:** brackets and `@` muted, the user in the
  speaker color, the node in a new pale turquoise.

### MRC lines carry an `[MRC]` badge (#930)

MRC lines now read `[MRC] <bob@Other>` instead of `<bob@Other (MRC)>`, which
since #916 looked like an alias for an account called "MRC". The site after
the `@` has its own pale lavender, never used for a linked node, so an MRC
caller does not pass for a Link caller. Search results and `/names` say
`bob@Other (on MRC)`, as `/who` does. Existing scrollback shows the new form;
nothing is rewritten.

### Long chat lines scroll (#926, #930)

A chat line wider than the terminal used to wrap on the last row and overwrite
itself. In channels, MRC rooms and direct chat, the input row now scrolls
sideways around the cursor. Tab completion works while it scrolls, and a
message arriving mid-line redraws the same stretch of your text with the
cursor where it was. (#973 later gave every single-line prompt the same
sideways scrolling.)

### For SysOps

- **Node rename warns about look-alikes (#906).** Setting this node's name to
  one that reads like a known node's is saved with a warning naming those
  nodes ("A node that knows both may warn its callers about this one, and chat
  will add each node's address to the name."). It is not refused.
- **Look-alikes raise the identity caution (#906).** The existing "familiar
  node name has a different cryptographic identity" notice now also fires for
  a look-alike of a known peer, an introduced node, or this node's own name
  and earlier names. DNS names are still compared exactly.
- Node friendly names were **not** restricted to the alias character set:
  that would make this node reject the hello of existing peers whose names use
  those characters (design doc §16, #899).

Nothing to do on upgrade. No migration, no new config key.

Related chat changes from the mail follow-ups, covered in the mail section:
blocks now stop live messages and `/invite` (#946, #951); the pending
invitation count moves into the first main menu (#933).

## Terminals

NetBBS now speaks to classic BBS terminals such as SyncTERM in their own
character set, CP437, and to terminals that show nothing but plain 7-bit
ASCII. The maintainer checked the result with a real SyncTERM 1.9 over
Telnet and SSH, including Zmodem in both directions.

### CP437 and ASCII terminals (#929, part)

This is steps 1 and 2 of #929 (#934, #936, #940, #941, #942, #943, #952).
**#929 stays open.** What is not done yet:

- **SysOp art is still decoded to Unicode on load** and mapped back per
  session. The uploaded bytes are not kept, and SAUCE records are not read
  (step 3).
- **No art with live slots on menus** (step 4): a SysOp cannot yet draw a
  main menu or a Boards, Chat or Files list and have NetBBS fill in the
  caller's own items and values.
- **No hand-drawn menu items** (step 5), and **no opening animation or
  line-speed playback** (step 6).
- **No CTerm device-attributes probe.** Detection uses the terminal type
  only.

For callers:

- **Four character sets.** Each session now sends its text in UTF-8, CP437 or
  plain ASCII. Screens are still composed the same way; the text is mapped
  as it goes out. Frames, rules and symbols become their CP437 equivalents,
  or ASCII ones (`+`, `|`, `-`, `=`) where CP437 has none. A replacement
  always keeps the width of the character it replaces, so columns stay
  lined up.
- **Text other callers wrote** is shown as the exact CP437 character where
  one exists, then the letter without its accent, then a small fold table
  (`ß`, `ø`, `ł`, currency signs and similar), and `?` as a last resort.
  ASCII is true 7-bit: accented letters become their base letters.
- **Cut text ends in `...`** on CP437 and ASCII terminals, and in `…` on
  UTF-8 ones.
- **Typing in CP437 works.** Accented letters and other upper-half bytes
  from a CP437 terminal are read one byte per character in every prompt,
  editor and break-in chat. Before, `é` from SyncTERM was dropped, and a
  box character could swallow the letters after it.
- **Detection before the first screen.**
  - Telnet now asks for the terminal type (TTYPE) and waits at most one
    second for the answer. Keys typed during the wait are kept.
  - SSH uses the terminal type of the PTY request.
  - `syncterm`, `ansi-bbs`, `pcansi` and `cterm` mean CP437. `ansi` means
    CP437 but leaves the choice open. Modern names (`xterm*`, `vt1xx`-`vt5xx`,
    `linux`, `screen`, `tmux`, `putty` and others) mean UTF-8.
  - A Telnet client that names no known terminal, refuses or doesn't answer
    gets **ASCII for everything, the SysOp's welcome banner included**. Over
    SSH an unknown name keeps UTF-8.
  - SSH's two pre-authentication messages, the welcome banner and the
    pending-approval notice, are now plain ASCII on every client, since no
    terminal type is known that early.
  - The web terminal stays UTF-8.
- **"Which of these lines looks right on your screen?"** replaces "Does that
  look garbled?" after login. It shows the same sample frame once in UTF-8
  and once in CP437, plus "Neither of them" (ASCII). It is asked only when
  detection wasn't sure and the caller has never chosen. The answer is saved
  and used at once.
- **Profile → `[U]nicode or CP437`** (the *Character set* field) cycles Auto,
  Unicode, CP437 and ASCII. Auto, the default, shows what it currently
  resolves to, such as "Auto (now CP437)". A choice made here beats
  detection. In the browser only ASCII changes anything.
- **Existing accounts:** an account that had switched the old Unicode style
  off reads as ASCII; every other account reads as Auto. No migration: the
  preference is stored under a new per-user key.

Doors:

- **Doors are transcoded between their own encoding and the caller's
  character set** (`DoorTerminal` in `src/netbbs/doors/runtime.py`):
  - a CP437 door on a CP437 terminal gets and sends bytes untouched;
  - a CP437 door is converted for a UTF-8 terminal, as before, and mapped
    for an ASCII one, where its frames now keep their shape as `+`, `|`,
    `-` and `=` (#952) instead of turning into `?`;
  - a UTF-8 door, such as the bundled Voidrunner, War Dialer and Retro
    Trivia, is mapped for CP437 and ASCII terminals, and a character split
    across two output chunks survives;
  - keystrokes are re-encoded into the door's encoding: CP437 typing reaches
    a UTF-8 door as UTF-8, and the reverse;
  - a raw door is passed through untouched both ways.
- The maintainer checked the three bundled doors in SyncTERM; they render
  cleanly.

For SysOps:

- **One log line per Telnet and SSH connection** says what the terminal
  reported and what was chosen, for example
  `telnet caller 203.0.113.9 terminal type: 'syncterm' (answered); character set cp437 (certain)`.
  Reported names are cut to printable ASCII and at most 40 characters.
- The TTYPE wait (one second) is a `TelnetServer` argument, not a
  `netbbs.toml` key.

### SyncTERM (#964)

The maintainer's SyncTERM 1.9 field test found 14 problems; all are fixed and
re-checked in SyncTERM (#965, #966, #967, #968, #971, #972, #973). Three of
them (the console alignment and picker items below) showed in PuTTY too.

For callers:

- **No more double-spaced screens** (#968). SyncTERM, like DOS ANSI.SYS, moves
  to the next line as soon as it writes the last column, so every
  full-width row left a blank line under it. On such terminals NetBBS now
  lays out every screen one column narrower than the terminal reports.
  - Only a terminal recognised as a modern UTF-8 emulator is trusted to
    wait. A CP437 name, `ansi`, an unknown name, no name at all, or a caller
    who chose CP437 gets the narrower layout. A modern terminal that doesn't
    say what it is loses one column.
  - Art keeps its full width: banners, the SysOp's banner previews and
    board art posts (#972) are drawn at the real width, and no line feed is
    sent after a row that fills it. Plain trailing spaces are trimmed from
    banner rows; spaces painted with a background color stay.
  - Nothing writes the bottom-right cell on these terminals, so the ANSI art
    editor's status line no longer scrolls the screen on every key. The art
    canvas stays 80 columns.
  - Doors are still told the terminal's real size.
- **SyncTERM's editing keys work** (#965): End (`ESC[K`), Page Up (`ESC[V`),
  Page Down (`ESC[U`), Insert (`ESC[@`) and Delete (0x7F). This applies only
  when the terminal reported `syncterm` or `ansi-bbs`. Everywhere else 0x7F
  stays Backspace, as PuTTY and xterm send it. Function keys are not read,
  and doors still get the raw bytes. On SyncTERM, Backspace at a one-key menu
  still opens help, because SyncTERM sends the same byte as Ctrl-H.
- **Long answers scroll sideways instead of wrapping** (#973). Every
  single-line prompt now keeps its answer on the prompt's row and scrolls,
  as chat input already did, so Backspace reaches the whole answer. This
  covers login and signup fields, passwords (the `*` stop at the edge),
  search prompts, typed confirmations such as deleting a door, and console
  text prompts. An answer that fits echoes exactly as before.
- **Uploads** (#972):
  - the "Uploaded 'x' (size) to Area." line shows the area name bold, in the
    node's accent color;
  - a Zmodem upload now returns to the file list, read again with the cursor
    on the new file, instead of the area list, and a Zmodem upload to a
    moderated area now says it waits for approval;
  - a browser-link upload shows in the list at its next redraw, without
    Ctrl-L.

For SysOps:

- **SSH works from SyncTERM.**
  - **A second SSH host key, RSA** (#966). SyncTERM 1.9's SSH library has no
    Ed25519 and failed with "Error -20 activating session". Each node now
    also has a 3072-bit RSA host key, offered as `rsa-sha2-512` and
    `rsa-sha2-256` only, never SHA-1 `ssh-rsa`.
    - **It is created on the first start after the upgrade** (the first
      start whose SSH listener finds it missing), no action needed.
    - It lives beside the database as `<database name without .db>_ssh_host_key_rsa`,
      for example `netbbs_ssh_host_key_rsa`, next to the existing
      `netbbs_ssh_host_key`.
    - **Callers' known_hosts entries don't change.** Ed25519 stays first in
      the server's order, and OpenSSH, PuTTY and similar clients keep the
      Ed25519 key they already trust. The server doesn't advertise the new
      key to clients that are already connected. A client that can only use
      RSA sees a new fingerprint on its first connection, which is the first
      time it could connect at all.
    - Backups now include the RSA key. Restoring an older backup leaves the
      RSA key in place.
  - **"Error -30 activating session"** (#971): before login the server no
    longer sends an empty SSH IGNORE packet ahead of each packet, which
    exceeded the SSH library's limit on no-op packets in a row. After login
    nothing changes. This is safe because the server offers no CBC cipher.
  - The SysOp handbook's troubleshooting table has a row for both errors.
- **SysOp console alignment** (#967): the dashboard's Quick column, and the
  second column of the Manage message boards, file areas and chat channels
  screens ("[R]evoke moderator") and of Settings ("[P]olicy trust"), were
  one position out of line wherever a description was cut to fit. Fixed in
  the shared menu grid, so every menu benefits.
- **Outcomes above the prompt in every picker** (#967). An outcome, such as
  "door registered", used to be printed above the picker's title, pushing
  the whole screen down a row until the next redraw. It now sits directly
  above `Choice:`. The Who screen's hint now reads "Select a session to
  disconnect it."
- **Snoop and break-in wrap like the caller's terminal** (#972), so the
  SysOp's view of a SyncTERM caller matches their screen after full-width
  output such as a door.

### Zmodem (#963)

- **Zmodem now works with real terminals** (#969, #970). Before this release
  it had only ever talked to NetBBS's own implementation, and no real
  client could start a transfer.
  - A download opens the way `sz` does (`rz` and a hex ZRQINIT), so
    SyncTERM and other terminals start receiving by themselves.
  - An upload opens with a hex ZRINIT, so the terminal offers its upload by
    itself. If it doesn't, start the terminal's Zmodem send.
  - Hex and binary headers, with CRC-16 or CRC-32, are read everywhere.
  - Resume and resend on ZRPOS, streaming in frames of up to 32 KiB made of
    1024-byte subpackets, and the proper ZFIN/`OO` ending are supported.
  - The receiver asks for damaged data again, at most 10 times per
    transfer.
  - **Ctrl-X five times cancels.** The upload and download prompts now say
    so. When NetBBS gives up it sends ten Ctrl-X and ten backspaces, as
    lrzsz does, so the terminal stops too.
- **Checked against real lrzsz 0.12.21rc** on Debian (60 tests), and by the
  maintainer with SyncTERM 1.9, download and upload.
- **Still one file per transfer.** Further files in a batch upload are
  skipped. Run-length encoding, compression, ZCOMMAND and sending with
  CRC-32 are not supported.

### Smaller fixes

- **A typed "²" can't end a session** (#935, #928). Digits such as `²` or
  fullwidth digits pass Python's `isdigit()` but not `int()`. Every number
  prompt now checks for ASCII digits. Three real crashes are fixed:
  Previous callers → [M]ail a caller (the caller's session ended; it now
  says "There is no caller ² on this list."), the update check on a release
  tag such as `v7.14²`, and a reliable-nodes fetch with such a
  `Content-Length`.
- **Keys in prompts are highlighted** (#975, #974). The ANSI art editor's
  "Unsaved changes. [S]ave, [D]iscard, or [C]ancel?", the fullscreen
  editor's quit and erase prompts, and the Node keys screen's `[R]otate` and
  `[C]ompromised` labels now color their keys the way menus do. The brackets
  stay, for ASCII and colorless terminals.
- **Static web files on NetBSD** (#962, #961). The web terminal's files, such
  as `xterm.js`, could reach a remote browser with parts overwritten, which
  broke the web terminal. On systems without `os.sendfile` (NetBSD, and
  Windows) NetBBS now uses aiohttp's own chunked sending. Not yet checked on
  a NetBSD node itself.

## Doors

### DoorParty template fixed after live use (#959, closes #566)

- **`remote-doorparty` and `remote-tunnel` are now 80x24**, not 80x25. The
  first real launch on ReLink from an ordinary 80x24 terminal was refused,
  because a door refuses a caller whose terminal is smaller than its fixed
  size. DOS templates stay 80x25.
- **MANUAL:** a DoorParty or tunnel door registered from the old template
  keeps 80x25 (applying a template copies its values). Set its height to 24
  in **Content → Doors** → the door → **[C]ompatibility → [H] Rows**, then
  **[S]ave**, or 80x24 callers stay refused.
- **Door guide:** the DoorParty section now records what was verified live
  (date, version, endpoints, identity mapping, tag format), what a wrong
  secret looks like (it connects, then `Invalid password…` and the door ends),
  and that `max_sessions: 1` on a remote door only limits this BBS to one
  caller at a time.
- **Back up the DoorParty credential file yourself.** `netbbs.backup` does not
  include it, and losing it orphans every caller's DoorParty account.
- The remaining unverified DoorParty items move to #958.

### BBSLink connector (#960, #565)

BBSLink does not speak RLogin, so it has its own adapter, `bbslink`, and a
`remote-bbslink` template. For each caller NetBBS gets a one-time token from
`games.bbslink.net:80`, sends an authorisation request, then opens Telnet to
`games.bbslink.net:23` and relays it.

**Setting it up**

1. **MANUAL — outside NetBBS:** apply at <https://www.bbslink.net/>. BBSLink
   e-mails a system code, an authorisation code and a scheme code.
2. **MANUAL:** copy `examples/doors/remote/bbslink.credentials.example.json`
   to `/var/lib/netbbs/bbslink.credentials.json` (or any absolute path, set as
   `credential_file`). Fill in `system_code`, `auth_code` and `scheme_code`,
   and nothing else, then `chmod 600`. The file must be a regular file of at
   most 4 KiB, not readable by group or others. **Back it up yourself**;
   `netbbs.backup` does not include it.
3. In **Content → Doors**, register a door, open it, and choose
   **[C]ompatibility → [P] Setup template** → `remote-bbslink`. Set `door`
   and the other options under **[Q] Adapter options**, then **[S]ave**.
   `door` is `menu` for BBSLink's own door menu, or one
   game's code (such as `lord`) to list that game directly; one registration
   per code. Keep `service_name` naming BBSLink: callers see it in the picker
   and before launch.
4. Run **[K] Check setup** on the same Compatibility screen. It refuses the file while a `REPLACE_WITH…` placeholder
   remains, or when it holds any other key.
5. Keep the door's play level above the guest account's. The caller's NetBBS
   user number is what keys their BBSLink player, so every guest would share
   one player.

**What BBSLink receives in clear:** your system code, the caller's user
number, the door code, the screen rows (24), a one-time token and a random request
key, plus "NetBBS" and this node's NetBBS version. The two
secret codes go only as MD5 hashes with that token. Nothing else about the
caller is sent. There is no TLS or tunnel route, so the template sets
`insecure_acknowledged: true`; a profile for the real service cannot be saved
without it.

**Limits and behaviour:**
- 80x24, cp437, one caller at a time. Raising `max_sessions` needs
  `multinode_certified`; two callers have not been tried on the live service.
- Both destinations must be in `allowed_destinations`; unknown options are
  refused.
- All three connections go to one resolved address, and two callers'
  handshakes run one after the other, because BBSLink ties the Telnet session
  to the authorisation by source address.
- Bounded: 2 s per connection attempt, 15 s for the whole handshake, 16 KiB
  per HTTP answer.
- Failures reach callers as "could not be started"; the reason is in the
  door's Last diagnostic (`BBSLink refused the session: …` is usually a
  mistyped code).

**Verified live (#565):** before release, the connector ran against the real
BBSLink service from a throwaway lab node, over SSH, Telnet and the web
terminal at 80x24: reaching LORD from the picker, the player identity, CP437
output, a clean exit, a provider that is down, refused codes and a second
caller turned away all passed. Not covered: a session long enough to hit the
time limit, 8-bit caller input, and two callers at once with `max_sessions`
raised.

**Do not reuse a node's codes on a test copy.** BBSLink keys players by system
code plus the NetBBS user number, so a lab or staging node using production
codes plays as the production node's callers.

### Door API

`door_api` stays **4**. No migration and no new netbbs.toml key; the new
settings are door-profile options (`bbslink` adapter) and the credential file.

## First-time SysOps

### Staff permissions: helpers without level 255 (#836: #851, #854, #863, #866, #868, #870)

Before this release, anyone who helped run a node needed level 255, and a
second 255 can demote, disable or delete the first. There are now three
**staff permissions** for an account below 255. Set them from the account's
detail screen with **`[S]taff`**, one toggle each. **Co-SysOp** sets all three
in one confirmed step, and the same screen can remove them all:

| Permission | What it allows |
|---|---|
| **Approve accounts** | Approve or decline pending signups. |
| **Manage accounts** | Disable and enable accounts, reset passwords, and set levels from 0 to 254. |
| **Moderate everything** | Moderator rights on every board, file area and chat channel. It also lets them read and post on every board and file area, whatever its level; age and verified-name gates still apply. |

- **Limits.** Staff act only on accounts below 255 that hold no staff
  permission themselves. They can't reach the SysOp, other staff, or their
  own account. These stay with a usable SysOp: raising anyone to 255,
  deleting accounts, granting staff permissions, granting the
  verify-identity permission, and granting moderator rights. Every change is
  checked when it is made, against a fresh read of the person making it. A
  permission revoked while a screen is open refuses the next action. Losing a
  permission returns a logged-in staff member to the main menu, which says
  what changed.
- **The Staff console.** A staff member sees **`[S]taff`** on the main menu
  instead of `[S]ysOp`. It counts what is waiting and offers only what their
  permissions reach:
  - **`[A]ccounts waiting`**, with approve accounts;
  - **`[U]sers`**, with manage accounts;
  - **`[M]oderation`**, when they moderate anything;
  - **`A[w]ay`**.

  It has no Settings, Operations, Link, Node, DNS or Backup screens. An
  account's detail screen offers a staff member only History, plus the
  actions their permissions allow. It never offers Key, Restrict, Staff,
  Identity or Delete.
- **Who is told about signups.** SysOps and approve-accounts holders see "N
  accounts awaiting approval: SysOp → Users" on the main menu. Staff see
  "Staff → Accounts waiting" instead.
- **Moderator grants.**
  - The grant screen's scope has a new **blanket across everything** option,
    which writes the board, file area and channel blanket grants in one step.
  - Two new presets, **Read and post** and **Read only**, are for boards and
    file areas, not channels. A read or write grant now lets its holder past
    that board's or area's minimum level. Age and verified-name gates still
    apply, and an approve or edit grant opens no level gate. Before, read and
    write grants did nothing, and the console never offered them.
- **Away notice.** `A[w]ay` sits on the SysOp console's landing screen and on
  the Staff console. It takes one line of plain text, up to 60 characters,
  with no pipe codes. You can add a return date as `YYYY-MM-DD`; the notice
  stops showing the day after it. Without a date, it reads "away since …".
  Being away changes nobody's permissions.

**Who can do what after upgrading:** every existing account starts with no
staff permissions, so no one gains account powers. Two things change
without any action from you:

- Members can now see existing moderators on the Staff list, with what each
  one moderates.
- Existing moderators with an approve grant get `Moder[a]tion (n)` on their
  main menu.

A second level-255 account still holds full power over the first. The
handbook now recommends staff permissions or a moderator grant instead.

### Signups (#850, #855)

- **The optional signup question.** Set it under **Users → Registration →
  `[Q]uestion`** (up to 200 characters; leave it blank to remove it). It is
  asked only when accounts need approval. A pending account's detail screen
  shows the question as it was asked and the caller's answer. Approving the
  account deletes the answer. Declining deletes the answer with the account.
- **`[D]ecline`** on a pending account's detail screen asks for confirmation,
  then deletes the account. The username is not held. Decline refuses if
  someone approved the account in the meantime.
- Reserved names and SysOp look-alikes apply to caller self-signup only. A
  SysOp can still create an `admin` account by hand.

### Content order (#857, #864, #856)

- **Communities, boards and file areas have a stored order.** On their console
  screens, `[U]p` and `[D]own` move them and a **Place** row shows where they
  sit. A board or area moves only among rows in the same category and
  Community, with the same pinned flag. **`[D]elete` is now `[R]emove`** on these screens, and it
  still asks you to type the name. Chat channels keep `[D]elete`. Console
  lists show the same order callers see.
- **On upgrade:** Communities keep their alphabetical order, and boards and
  file areas take the order they were created in. New ones, including
  carried ones, go last. Callers who never picked an order see yours instead
  of activity order.
- **A node with no content** shows the SysOp "No boards yet: create one under
  SysOp → Content." above the main-menu prompt.
- The Categories and Communities descriptions on the Content screen now say
  what each one is, and Categories answers Ctrl-H.

### The node log and first-day wording (#853, #884, #872, #892)

- **A caller hanging up is one INFO line** (`telnet caller 203.0.113.9
  disconnected`). It used to be an ERROR with a 40-line traceback. Real errors
  keep their traceback.
- **Probation is explained once per node or caller**, at INFO, instead of a
  WARNING on every sync. Blocks and quarantines stay at WARNING.
- **Startup explains itself.**
  - A warning appears when the web listener is on but no transfer links can be
    made (no `public_url`).
  - A note names the `host` line to add for each listener that only listens
    on loopback.
  - The Link identity line names the node and says the `[node] name` only
    labels the key files.
  - A hint says how to set a timezone when none is set.
- **The clock says `UTC`** on the main menu when the node's zone is UTC. The
  default is still UTC, so existing nodes' times don't move.
- **"Standalone mode" is gone.** The offline console says "The node isn't
  running…" or "This console runs outside the node…".
- **The default welcome banner shows the NetBBS Link line only when the node
  last ran with Link on.**
- **First SysOp setup** no longer says "leave blank to skip" for a password or
  key.
- **Developer text is gone from SysOp screens.** Help texts, the Join NetBBS
  Link note and the full-peer Link startup warning no longer cite design-doc
  sections or issue numbers.
- **The web server** answers with `Server: NetBBS` instead of its Python and
  aiohttp versions. A plain browser visit to `/ws` explains what the address
  is for.
- **Descriptions stay visible at 80x24.** When there's no room under each
  entry, the description goes on the entry's own line, cut to fit.
- **Every new account starts with redraw-in-place on.** This includes the
  first SysOp made at install.
- **A graceful restart with nobody on happens right away.** The countdown
  checks about once a second and ends as soon as the last caller leaves.
  Drain is unchanged. The delay is set in **Settings → Network & login limits**
  or `[shutdown] graceful_delay_seconds`.
- **The Level and shutdown-delay prompts** open with the current value ready
  to edit, like the Create and Edit screens. Leaving the level unchanged
  records nothing.

### Link probation, both ways (#887)

- **Link status → Peers and seeds** now shows how many peers are **on
  probation here**, and whether **your node** is on probation at the peers it
  dials. It explains that nothing is exchanged either way during probation.
  The console's LINK line shows `On probation: N` next to Peers.
- **A peer's screen** (Link status → Peers → node) explains probation and
  shows when it started (Known since). It says when probation ends by itself:
  no earlier than a date, after 3 days of contact and vouches from 2 trust
  domains, with progress so far. If no trusted reporter here vouches for
  nodes, which is normal on a hobby node, it says only Establish ends it.
- **An Exchange section** says what the peer sends and what your node sends.
  While the peer is on probation, **Offered, held back** lists its boards,
  channels and file areas. Establish now also releases everything held back
  from that node.
- **After `[L]ink`**, the announcement says which peers get it. A linked
  board, area or channel shows a line per verified peer, up to six and then "... and N more": `has it`,
  `refused: your node is on probation there…`, `nothing sent while it is
  probationary here`, `not yet` or `not known`. These are known only for
  peers your node dials, and they reset when the node restarts.
- The handbook now says that carried content appears in the Message boards,
  Chat and Files lists, outside any Community.

### Backups on the dashboard (#890)

- **The "Backup: never" line is gone from nested console screens**, where it
  read like an error about the screen you were on. Backup state stays on the
  landing dashboard, the Operations panel and the Backup screen.
- **The Backup page leads with backups.** The schedule sits right under the
  last and recent backups. On a node with no doors, the door sections become
  one line: "No doors are set up, so backups hold no door data."

### Banners and the art editor (#889)

- **Banners fit the screen.** Blank rows at the end of every banner and
  masthead are dropped when it is shown, including art saved by earlier
  versions. The SSH pre-login banner loses about 40 blank lines. Blank rows
  inside the art stay.
- **Editor fixes:**
  - The canvas repaints after the color or glyph picker, and **Ctrl+L**
    repaints too.
  - **Ctrl+K** clears from the cursor to the end of the row.
  - Typing at column 80 no longer wraps to the next row. The status line
    shows `Col 80/80 (end)`, and **End** goes just past the row's last
    character.
- **Preview** shows a saved but switched-off banner as saved, with a note
  that `[E]nable` turns it on. The diagnostic `enabled=…` lines are gone.
- **Three quiet welcome presets:** Paper & Ink / Sepia Letterhead, Library
  Card / Oak & Linen, and Garden Gate / Sage & Stone.
- **Settings → Pre[v]ious callers** cycles neon → plain → hidden. Plain uses
  the node's header color with no gradient.

### Install docs and handbooks (#849, #909, #927)

- **The website's Get started box and the handbook install steps** have been
  corrected where a first-time SysOp got stuck:
  - `VERSION` means the version number only.
  - Both check `python3 --version` and install `python3-venv` on Debian and
    Ubuntu.
  - The wheel is copied to `/tmp` so the service account can read it.
  - The `--version` check runs as the service account.
  - The config template shows `host` and `port` for Telnet and the web
    listener.
  - The reverse-proxy section has a complete Caddyfile, plus an nginx block
    with WebSocket headers, `proxy_read_timeout 1h` and
    `client_max_body_size 101m`.
  - `examples/netbbs.service` and `netbbs.rc` no longer point at PyPI.
- **The SysOp handbook** adds:
  - a first-day path: registration, levels, content, look, backups, then Link;
  - a Levels section. New accounts are level 0, levels 1-254 grant nothing by
    themselves, and a board that needs level 255 to post shuts out future
    helpers;
  - "Moderator, staff member or second SysOp?";
  - where Categories live, and an account lifecycle section.

## Link

### Phase 4 readiness record (#915)

Docs only. `docs/NetBBS-phase4-readiness.md` and the runbook in
`docs/NetBBS-link-dogfood-plan.md` record the trust and recovery exercise run
on ReLink, OutBound and The Emptiness Machine from 26 to 29 September.

- **Manual quarantine, block and recovery:** done, with one defect: recovery
  took effect only at a restart (#802).
- **Independently administered multi-node:** done, on three live nodes on
  three networks.
- **Key rotation and compromise response:** exercised; one requirement fails.
  A node that knows the rotating node only by introduction accepted a stale
  copy signed by the compromised key (#914).
- **Still private/experimental.** Phase 4 (#131) is not complete, and no
  public-readiness claim is made, while #914, #897 (one refused event wedges
  the whole push), #802 and #83 (the sustained dogfood run) stay open. All
  four are open at this release.
- Other open findings, about operation rather than the trust model: #700
  (retry waits), #745 (a `*` scope category authorizes nothing), #752 (the
  explanation below threshold), #860 (chat never reaches an outgoing-only
  origin).
- The runbook now folds in what the run taught: establish peers in every
  direction, keep the checking node stopped for before/after rows, use
  concrete scope categories, and pick vouch subjects the vouching node does
  not host.

### What crosses the Link

No new message type, field or protocol version outside mail.

- **Board post bodies (#913):** a post that points at a file in a file area
  goes over Link with one text line per file (name, size, area, this node's
  name) at the end of its body. No reference crosses Link. The body is still a
  plain string, so older nodes accept it and show the text.
- **Chat labels (#905, #906, #916):** display only. Link direct messages still
  carry `from_display_label`; it now holds `alias (username)`. Older peers
  show whatever the sender put there, and send the pipe form themselves.
- **Relay mailbox (#894, #891):** a relay now drops envelopes not collected
  within 30 days (a constant, not a setting). Local only; the sender learns
  of a lost letter from its own 14-day relay timeout (#895).
- **New-node probation (#887, #844):** what is held back, and what a peer
  refused, is tracked in memory and shown on SysOp screens. No wire change;
  a peer's existing 403 policy answer is now recognised as such.
- **Node log (#853), ASCII digits (#935):** local only.
- **Mail (covered in the mail section):** #804 (probation no longer swallows
  mail; relay-picked-up mail refused by policy becomes a signed bounce), #806,
  #807, #808, #816–#820, #824, #826–#828, #874, #919, #921.

## Upgrade and rollback

Take a backup, stop NetBBS, replace the wheel and start it. Nineteen
migrations run on the node database, taking it from schema 87 to 106:

- **The `signup_answers` table** (#855), empty. It holds a new caller's
  answer to the SysOp's signup question until the account is approved.
- **`position` on Communities** (#857), filled in today's alphabetical order,
  so nothing moves.
- **`position` on boards and file areas** (#864), filled in the order they
  were created; a trigger puts every new board or area, local or carried over
  the Link, last. Saved caller sort choices are kept, but **a caller who never
  chose an order now sees boards and file areas in this stored order instead
  of by activity.** The sort-preference table is rebuilt to allow the new
  `sysop` order.
- **Staff permissions** (#863, #870): `staff_permissions` on accounts, 0 for
  every existing account, so no account gains anything at upgrade; and an
  empty `staff_away` table for away notices.
- **Link mail delivery state** (#867, #895, #938): `link_delivery_reason`,
  `link_delivery_notice_pending`, `link_relay_handoff_at` and `resent_at` on
  mail, all empty or off for existing mail. Mail that bounced before the
  upgrade is not reported again, and a letter already handed to a relay keeps
  waiting as before rather than expiring after 14 days.
- **`from_system` on mail** (#881), off for every existing letter: rejection
  mail sent before the upgrade still shows the moderator as its sender.
- **The `link_mail_refusals` table** (#882), empty: refusals before the
  upgrade were not kept.
- **The `mail_blocks` table** (#883), empty.
- **The mail table is rebuilt** (#888) so that deleting an account no longer
  deletes the Sent copies of mail it received; a new `recipient_label` column
  starts empty. **The rebuild also tidies existing mail:** received Link mail,
  and mail from accounts already deleted, is marked as deleted on the sender's
  side, and such letters that their recipient had already deleted are removed
  now. No one could see those rows. A new `mail_eviction_notices` table starts
  empty.
- **The `mail_search` index** (#901). Every stored letter is indexed at
  upgrade, which takes longer on a node with a lot of mail.
- **`kept_at`, `mail_group_id` and `mail_group_to` on mail** (#908, #910),
  empty for every existing letter: nothing is in Kept, and every letter counts
  as sent to one person.
- **Read receipts** (#911, #932): `first_read_at` is copied from each read
  letter's read time, and `first_read_shared` is set for those letters between
  two local accounts, unless either account has receipts turned off. Receipts
  are on by default, so letters read before the upgrade show as read to their
  senders.
- **The `mail_file_refs` and `post_file_refs` tables** (#912, #913), empty.

Three settings are new, all stored in the database with defaults that keep
today's behavior: the signup question (`registration_question`, none), the
lowest level that may use mail (`mail_min_level`, 0), and the plain
previous-callers panel (`previous_callers_plain`, off). One permission does
change: **the guest account can no longer read or send mail**, whatever the
mail level says. Callers keep their Unicode choice: an account that had
switched the decorative style off starts on ASCII, every other one on Auto.
No key in `netbbs.toml` changed.

On its first start the node generates a second SSH host key, a 3072-bit RSA
key, beside the database as `<database name>_ssh_host_key_rsa` (#966). The
Ed25519 key stays first, so callers keep the fingerprint they know. Backups
include the new key.

The systemd and NetBSD rc.d examples changed only in their comments.

**Rolling back needs a restore.** A 7.13.0 wheel refuses to open a schema-106
database. **MANUAL — to roll back:** stop NetBBS, install the 7.13.0 wheel,
then restore the backup taken before the upgrade. Anything since the upgrade
is lost with it: mail sent or received, blocks, Kept letters, read receipts,
file links in mail and posts, the Link mail refusal log, signup answers, staff
permissions and away notices, the order of Communities, boards and file
areas, callers' character-set and mail-order choices, and the signup
question, mail level and previous-callers setting. The RSA host key file
stays on disk; 7.13.0 ignores it, so callers whose clients need RSA (older
SyncTERM builds) cannot connect over SSH until you upgrade again, when the
same key is used.

## Verification boundaries

- **The full test suite passed on the release tree** on Windows. The
  POSIX-only tests, the door runtime's PTY paths among them, ran only where
  individual PRs ran them, not as one suite on this tree.
- **Mail between nodes has been tested in-process and over loopback,** not
  on the live test network. No test runs a 7.13.0 node against this one;
  mixed-version behavior rests on the new bounce reasons and sealed-letter
  fields being ones 7.13.0 ignores, which was checked by reading its code.
- **The SyncTERM work was checked by the maintainer with SyncTERM 1.9** over
  Telnet and SSH, including Zmodem both ways, and against lrzsz on Debian.
  Other CP437 terminals (NetRunner, mTelnet, real DOS terminals) have not
  been tried. Character-set detection rests on the terminal types and
  replies those terminals are documented to send.
- **Static files on systems without `os.sendfile`** were fixed from a NetBSD
  report and tested on Windows, not yet on a NetBSD node.
- **BBSLink was verified against the live service from a lab node** running
  unreleased code (#565); no production node runs it yet. The long-session,
  8-bit-input and several-callers cases were not covered.
- **The staff permissions** were tested through the console and the account
  functions. The field test that asked for them has not been repeated on
  this release.
- **Known gaps, tracked or noted:**
  - CP437 art on menus, hand-drawn menu items and animation are not done
    (#929 stays open).
  - SSH host private keys are written with the process umask, often
    world-readable (#976). This predates the release and now covers the new
    RSA key too.
  - On a caller's first login after the upgrade, Link letters that were
    already unread can be counted once as "new since your last call": the
    migration that tidies mail stamps received Link mail with the upgrade
    time.
  - Phase 4 of the Link trust work is not complete: #914, #897, #802 and #83
    are open. See *Link*.

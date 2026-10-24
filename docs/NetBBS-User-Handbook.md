# NetBBS User Handbook

NetBBS is a text-based community with message boards, chat, mail, files,
and games. Each BBS is run by a **SysOp**: the person who manages that node,
sets its rules, and can help with your account.

[All documentation](README.md) · [Running a node](NetBBS-SysOp-Handbook.md)

## Connect and sign in

| At a glance | |
| --- | --- |
| **You need** | The web address or terminal connection details your SysOp provides |
| **New here?** | Type `new` at the username prompt |
| **Later** | Change your password under **Profile ▸ Password** |

#### Connect

Use the web address or terminal connection details your SysOp provides:

- **A browser** needs no terminal software.
- **SSH** looks like `ssh -p 2222 yourname@bbs.example.org`; substitute the
  actual port and address.

> [!TIP]
> Use SSH or HTTPS when available: plain Telnet does not encrypt your login.

#### Sign in or create an account

Log in with your account. If registration is offered, type `new` at the
username prompt (over SSH, connect as `new`).

Usernames use:

- the letters A-Z without accents, digits, `_`, `-` and `.`;
- up to 32 characters.

The name is checked before you choose a password.

Some nodes require approval before you can log in. Until the SysOp approves
your account, logging in tells you it is still waiting, and you cannot look
around in the meantime. If registration is closed, contact the SysOp.

> [!NOTE]
> Accounts and access rules belong to each node.

#### Change your password

Under **Profile ▸ Password**, you type the current password, then the new one
twice, and nothing is echoed.

If you have forgotten it, ask the SysOp to set a new one; nobody can recover
the old one.

#### Change your username

Your username is the name you log in with, and who you are to other nodes on
NetBBS Link, so the SysOp decides on a new one.

1. Open **Profile ▸ Username** and type the name you would like. It is
   checked at once, as a new account's name is.
2. The SysOp approves or refuses it. Until then you keep your current name.
   Typing another name replaces your request; a blank line withdraws it.
3. If it is refused, you see why when you next log in.
4. If it is approved, you are logged out with a line naming your new
   username. Log in again with it.

What changes and what stays:

- Mail written to your old name from other nodes or an FTN network still
  reaches you, and so does a `/msg`. On this node, write to your new name.
- Your posts, files and sent mail keep the name they were written under.
  Chat lines you wrote show your new name.
- The SysOp approves one change every 30 days.

## Find your way around

| At a glance | |
| --- | --- |
| **Choose** | Press a highlighted letter, such as <kbd>M</kbd> for **Message boards** |
| **Go back** | <kbd>B</kbd> or <kbd>Esc</kbd> |
| **Stuck?** | <kbd>?</kbd> shows help for the screen you are on |

In this chapter: [Moving around](#moving-around) · [The main menu](#the-main-menu) ·
[Lists of boards and areas](#lists-of-boards-and-areas)

#### Moving around

Press a highlighted letter such as <kbd>M</kbd> for **Message boards**. Most
menus react immediately, without <kbd>Enter</kbd>. In the browser you can
also click an entry or a numbered row.

- **In a list**, type a row's number: two digits (`03`) or one digit and
  Enter (`3` <kbd>Enter</kbd>).
- **Settings screens** such as **Profile** number their settings the same
  way: type a setting's number, or move to it with <kbd>Up</kbd>/<kbd>Down</kbd>
  and press <kbd>Enter</kbd>, and <kbd>Left</kbd>/<kbd>Right</kbd> step the
  highlighted one's value. The numbers run on through a screen's sections, so
  a number works from any page of it.
- **A pause** that waits for you reads `[Enter] Continue`: any key goes on,
  and in the browser so does a click on it.
- **When typing text**, use <kbd>Enter</kbd> to submit it. <kbd>Ctrl+U</kbd>
  clears the line you are typing, and at a field that allows it,
  <kbd>Esc</kbd> leaves it without changing anything.

Follow the action bar on the current screen: letters can mean different
things in different places. A few keys mean the same on every screen:

| Key | What it does |
| --- | --- |
| <kbd>B</kbd> or <kbd>Esc</kbd> | Back, one level up: from a board to the list you picked it from, and from there to the menu. <kbd>Esc</kbd> first clears a highlighted row, if there is one |
| <kbd>&lt;</kbd> <kbd>&gt;</kbd> | The previous or next page of a list or a long text; <kbd>PgUp</kbd>/<kbd>PgDn</kbd> do the same, and so do <kbd>Left</kbd>/<kbd>Right</kbd> on a list |
| <kbd>/</kbd> | Find, wherever a list can be searched |
| <kbd>?</kbd> | Help for the screen you are on; <kbd>F1</kbd> and <kbd>Ctrl-H</kbd> do the same |

> [!TIP]
> **Help** (<kbd>?</kbd>) on the main menu sums up how the board works and
> who runs it. To write to the SysOp, send E-mail **To: sysop**.

#### The main menu

| Main-menu choice | What it does |
| --- | --- |
| <kbd>M</kbd> **Message boards** | Every message board on this node |
| <kbd>C</kbd> **Chat** | Every chat channel |
| <kbd>F</kbd> **Files** | Every file area |
| <kbd>G</kbd> **Games** | Door games, when the SysOp has set some up |
| <kbd>T</kbd> **Topics** | The node's Communities: topics the SysOp set up, each with its own boards, chat channels, file areas and games |
| <kbd>N</kbd> **New scan** | See unread activity and resources you have not visited (below) |
| **Find** (<kbd>/</kbd>) | Search your own mail and the posts, files and retained chat on this node (below) |
| <kbd>E</kbd> **E-mail** | Read and send persistent NetBBS mail, from the level the SysOp chose; not offered to the guest account |
| <kbd>W</kbd> **Who's online** | See callers and available ways to contact them |
| <kbd>R</kbd> **Recent callers** | Who called lately, and the newest lines on the oneliner wall (below) |
| <kbd>P</kbd> **Profile** | Change your public profile and personal preferences |
| **Approvals** (<kbd>A</kbd>) | Posts and uploads waiting for your decision, if the SysOp made you a moderator |
| <kbd>S</kbd> **Staff** | Your staff console, if the SysOp gave you staff permissions |
| <kbd>O</kbd> **Operators** | Who runs this node: the SysOps, staff and moderators, when each was last on, and who is away |

Some choices appear only when they have something to show or you have
permission to use them.

**New scan:**

- Back from each one brings you back with the next new one ready for
  <kbd>Enter</kbd>.
- <kbd>R</kbd> **Replies** opens the replies to your posts.
- <kbd>E</kbd> **E-mail** opens your mailbox, whose unread count is shown
  above the list.

**Find** searches your own mail (your Inbox and Sent, never anyone else's)
and the posts, files and retained chat on this node.

- Mail results come first, up to 20 of each kind.
- A letter opens as it does in the mailbox, and Back returns to the results.
- Mail is left out where the SysOp has not opened mail to you.

> [!NOTE]
> Search covers content held by this node, including carried NetBBS Link
> content; it is not a search of the entire network.

#### The oneliner wall

The oneliner wall is a list of short lines callers leave for the callers
after them. After you sign in, its newest lines are shown under
**Recent callers**, on the same screen and with one pause.

To add a line:

1. Press <kbd>A</kbd> **Add a line**, at that pause or on
   ▸ **Recent callers**.
2. Type your line, up to 60 columns, and press <kbd>Enter</kbd>.
   <kbd>Esc</kbd> or an empty line cancels.

Any other key at the pause goes on to the main menu, where the result is
shown.

- You can add one line per call, and three a day. Deleting a line does not
  give you one back.
- A line is plain text: colour codes are not drawn.
- The SysOp may set a level for adding lines. Below it, **Add a line** is
  not offered, but you can still read the wall.

▸ **Recent callers** ▸ <kbd>O</kbd> **Oneliners** shows the whole wall,
newest first. Page it with <kbd>&lt;</kbd> and <kbd>&gt;</kbd>, and press
<kbd>/</kbd> to find a name or a word. To remove one of your own lines,
press <kbd>D</kbd> **Delete**, type its number and answer <kbd>Y</kbd>.

> [!NOTE]
> Your line shows your account name, even if you hide your name from
> Recent callers. The SysOp and staff who moderate everything can delete
> any line.

#### Lists of boards and areas

Lists of boards and file areas keep the SysOp's order, so the number you
remember for a board still picks it next time.

- <kbd>O</kbd> **Order** in a list sorts it another way, by activity, name,
  newest or size, just this once or from then on;
  <kbd>S</kbd> **SysOp's order** there puts it back.
- **Find** (<kbd>/</kbd>) in a list shows only the names containing what you
  type; a blank answer shows the whole list again.

A board, file area or chat channel whose name is drawn in pale turquoise is
**Linked**: shared with other nodes over NetBBS Link. If your terminal shows
plain ASCII, or the SysOp drew the list as art, its name ends in ` ~`
instead.

## Read and post

| At a glance | |
| --- | --- |
| **Where** | <kbd>M</kbd> **Message boards**, or <kbd>E</kbd> **E-mail** for letters, on the main menu |
| **Try first** | Open a board, select a post, and reply to it |
| **While writing** | The editor shows its commands; in the fullscreen editor <kbd>Ctrl+G</kbd> shows help |

In this chapter: [Read and reply](#read-and-reply) ·
[Polls](#polls) ·
[Point a post at files](#point-a-post-at-files) ·
[Post to several boards](#post-to-several-boards) ·
[Read offline (QWK)](#read-offline-qwk) ·
[The line editor](#the-line-editor) ·
[The fullscreen editor](#the-fullscreen-editor) ·
[Drafts](#drafts-come-back-where-you-started) ·
[Address a letter](#address-a-letter) ·
[Send netmail](#send-netmail-to-an-ftn-network) ·
[Point a letter at files](#point-a-letter-at-files) ·
[Review and send](#review-and-send)

#### Read and reply

Open a message board, select a post, and use the displayed actions to read,
reply, or start a topic. Follow a resource to make it easier to revisit.

A post keeps the lines you write: a list, a short line or your sign-off stays
as you typed it, and only a line too wide for the reader's screen wraps.

**ROT-13.** Some posts hide a spoiler or a punchline in ROT-13, which turns
each letter 13 places along the alphabet, so `Uryyb` reads `Hello`. Press
<kbd>D</kbd> **Decode ROT-13: off** while reading one to show it decoded,
and <kbd>D</kbd> **Decode ROT-13: on** to show it as written again. Only
your screen changes: the post stays as its author wrote it, and the next
post you open shows as written. A letter has the same key.

On a moderated board, your post may wait for approval before others see it.
You are told at the main menu when it is approved or rejected; a rejection
also arrives as mail from **System**, with the reason and what you wrote.

**Echoes.** Some boards may carry an **echo** from an FTN network such as
FidoNet or fsxNet. People on many other BBSes read and write in an echo, and
what you post there goes out to them too.

- Their posts are signed with a name and an FTN address, as in
  `Joe Bloggs (21:3/110)`.
- **Reply** works as on any board.
- Your posts go out under your username, except in an echo that shows
  verified names beside posts. Some echoes take real names only, so there
  your post goes out under your verified real name. Without one, or with one
  longer than the network allows (35 bytes), the board is read only for you
  and says why. Ask the SysOp to verify your name, or a shorter form of it.

> [!NOTE]
> In an echo, an edit or a deletion stays on this BBS, because other BBSes
> keep the copy they already received.

**QWK network conferences.** A board may instead carry a conference of a QWK
network, such as DOVE-Net, which many Synchronet BBSes share. It works like
an echo: what you post goes out to the other BBSes under your username, with
a line naming this BBS at the end, and their posts are signed with a name and
the ID of the BBS they were written on, as in `Digital Man (VERT)`. An edit or
a deletion stays on this BBS.

#### Polls

A poll asks the board a question with answers to vote on.

1. On a board's list press <kbd>N</kbd> **New poll**.
2. Type the question and two to ten answers, and choose whether voters pick one
   answer or up to a number of them, when the poll closes (seven days ahead
   unless you change it), and whether results show after voting or only after
   the poll closes.
3. <kbd>N</kbd> **Next**, write the post that introduces the poll, and
   <kbd>P</kbd> **Post** it on the review screen.

Reading a poll's post shows the poll under it. <kbd>V</kbd> **Vote** ticks
your answers; you can change your vote until the poll closes. Results show
once you have voted, or straight away if you cannot vote there, or only when
the poll closes if its author chose that. The poll's author and the board's
moderators can end it early with <kbd>C</kbd> **Close poll**.

- Once anyone has voted, the answers can no longer change. The closing date
  can, with <kbd>E</kbd> **Edit**.
- Withdrawing the post, or a moderator taking it down, closes the poll and
  hides its results.
- A board that carries an echo or a QWK network conference offers no polls:
  the other BBSes could not show them.

> [!IMPORTANT]
> **Who can see your vote.** No screen shows who voted what -- not to the poll's
> author, a moderator or the SysOp: only totals and the number of voters. But
> this BBS's database records each account's vote, and a SysOp with the
> database file can read it. On a board shared with other BBSes your vote also
> travels to every BBS that carries the board, signed with your name, and each
> of their operators can read it the same way. Such a poll says "Your vote is
> shared with every BBS that carries this board." before you vote.

#### Point a post at files

A post can point at files that are already in a file area here, up to five --
the practice page you want an opinion on, say.

1. On the review screen press <kbd>A</kbd> **Attach file**.
2. Choose the file area and then the file.

<kbd>R</kbd> **Remove file** takes one off again.

Nothing is copied: the post names the file, and whoever reads it downloads it
from its file area. The files are listed under the post's byline, and
<kbd>G</kbd> **Get file** downloads one -- with several, you choose which --
by Zmodem, or with a browser link where your terminal has no Zmodem.

- A reader who may not open that file area sees only that a file is there,
  not its name; the review screen tells you when a file's area is closed to
  some of the board's readers.
- A file deleted or expired since shows as no longer available.
- When you edit a post, its files come along and can be changed on the
  review screen.
- On a board shared with other BBSes, readers there get a line naming each
  file, its size, its file area and this BBS at the end of the post, not a
  download.
- A draft keeps the text, not the files.

#### Post to several boards

A new post can go to more than one board at once -- a question that belongs
in two places, say. Replies and edits always stay on one board.

1. On the review screen press <kbd>T</kbd> **To boards**.
2. Tick each board the post should go to, and press <kbd>B</kbd> **Back**.

The board you wrote it on is always ticked. A row under the subject lists the
boards, and nothing is posted until you press <kbd>P</kbd> **Post**.

- **How many:** the SysOp sets how many boards one post may go to, five
  unless they changed it, counting the board you wrote it on. The list tells
  you when you reach it.
- **Boards you can't use:** the list shows them dimmed, with `-` instead of a
  number and the reason beside the name -- a level you don't have yet, say,
  or a board without color for an art post.
- **All or nothing:** if one of the boards refuses the post when you press
  **Post**, nothing is posted anywhere; the review screen names the board and
  why, and **To boards** lets you take it off.
- **One copy on each board:** each board gets its own copy, with its own
  replies. A moderated board may hold its copy for approval while the others
  appear at once. Editing or withdrawing a copy changes only that one.
- **Read once:** opening one copy marks the others read for you too, and new
  scan and search count the post once. A copy's byline names the other boards
  it is on, as in "also in Showcase", when you can open them.
- **Shared and FTN boards:** a copy on a board shared with other BBSes goes
  out like any post there. In an echo, the copy starts with a line such as
  `* Crossposted in FSX_BBS` naming the other echoes of that network it went
  to.

#### Read offline (QWK)

A **QWK packet** holds your new mail and the new posts on the boards you
follow, to read and answer offline in a QWK reader such as MultiMail. The
reader makes a **reply packet** of what you wrote, and uploading it posts
your replies here.

1. Press <kbd>M</kbd> **Message boards**, then <kbd>Q</kbd> **QWK offline
   mail**.
2. Press <kbd>D</kbd> **Download packet**. It arrives as one file named
   after this BBS, as in `RELINK.QWK`.
3. Open it in your reader, read, and reply.
4. Back here, press <kbd>U</kbd> **Upload replies** and send the reply packet
   the reader made, as in `RELINK.REP`.

Both transfers go the way a file area's do: by Zmodem, or by a browser link
when your terminal has no Zmodem (<kbd>W</kbd> **Web transfer** asks for one
on purpose). The screen shows what is waiting and the packet's name.

- **What a packet holds:** your unread letters as conference 0, then the
  unread posts on each board you follow, oldest first. Each board keeps the
  conference number it got in your first packet, so your reader files it in
  the same place every time.
- **Choosing boards:** <kbd>C</kbd> **Conferences** lists every board you can
  read. Tick a board to take it into your packets, or untick one you follow
  but would rather read online, then press <kbd>S</kbd> **Save**. What you
  follow doesn't change. A board you have never opened starts with the posts
  that arrive after your first packet, as a first visit would.
- **Big backlogs:** a packet holds up to 2,000 messages unless the SysOp set
  another number. The rest wait for your next packet, and the screen says so.
- **Read or not:** downloading marks nothing read. The messages a packet
  carried are marked read when your next reply packet arrives, or when you
  press <kbd>M</kbd> **Mark read**. Download again before either, and you are
  asked whether to include the same messages again; say yes if the packet
  never reached your reader.
- **Your replies:** each reply is posted as if you had written it here, under
  your own name, with the same rules: a board you can't post on refuses it, a
  moderated board holds it for approval, and a post on a shared or FTN board
  goes out to the other BBSes. A reply to a letter goes back to whoever wrote
  it; a new letter in conference 0 goes to the name in its To line. A board
  has no private messages, so write those as letters.
- **What became of them:** after an upload the screen says how many replies
  went out, and <kbd>R</kbd> **Report** lists each one -- posted, waiting for
  approval, sent, or refused and why. Uploading the same reply packet twice
  posts it once.

> [!NOTE]
> Readers that understand QWKE, MultiMail among them, keep names and
> subjects longer than QWK's 25 characters. Dropping a conference in such a
> reader takes the board out of your packets here too.

#### The line editor

The text editor shows its commands. In the line editor:

| Type | What it does |
| --- | --- |
| A blank line | Starts a new paragraph |
| <kbd>Enter</kbd> on an empty line twice, or `/done` | Finishes |
| `/insert N` | Writes the next lines before line N until you type `/end` -- this is how you answer between the quoted lines of a reply |
| `/list` | Shows every line and where you are writing |
| `/unquote` | Removes the whole quote a reply opens with |
| `/delete N-M` | Removes lines N to M, so you can keep only the part you answer |
| `/exit` or `/quit` | Keeps a draft for later |
| `/cancel` | Discards it |

> [!TIP]
> Both editors keep what you have typed if the connection drops.

#### The fullscreen editor

In the fullscreen editor, <kbd>Ctrl+G</kbd> shows help and
<kbd>Ctrl+X</kbd> opens the exit choices, including **Keep draft & exit**.
It also has these keys:

| Key | What it does |
| --- | --- |
| <kbd>Ctrl+K</kbd> | Cut the line. Press it again to add the next line. |
| <kbd>Ctrl+Y</kbd> | Paste the cut lines. |
| <kbd>Ctrl+E</kbd> | Erase all the text, after a yes. <kbd>Ctrl+Y</kbd> brings it back. |
| <kbd>Ctrl+W</kbd> | Delete the word before the cursor. In a browser, which closes the tab on <kbd>Ctrl+W</kbd>, use <kbd>Alt+Backspace</kbd>. |
| <kbd>Ctrl+R</kbd> | Rewrap the quoted (`>`) paragraph under the cursor to fit the screen, keeping its `>`. |

It shows what you are writing above the text: the recipient or board, and
the subject. The status line shows how many characters you have used and the
limit. The limit assumes the rest is plain letters: an accented letter or
other special character takes more room and lowers it by one.

Turn the fullscreen editor on or off in **Profile** with
**Fullscreen editor (all writing)**. It is used for mail, posts, your bio and
signature, and file descriptions.

#### Drafts come back where you started

A draft comes back where you started it:

- A new post's draft is offered when you return to that board.
- A reply's draft is offered when you reply to the same post or message
  again.
- An unfinished letter is shown on the mail screen under <kbd>D</kbd> **Draft**,
  with its recipient and subject, and <kbd>C</kbd> **Compose** offers it
  before starting a new one: resume it, delete it, or leave it for later.

A draft is never sent or posted by itself.

#### Address a letter

Writing a message or a post opens a screen of its own. Mail asks who it is
for first: a user name, or `name@TheirBBS` for someone on a linked BBS. An
empty line or <kbd>Esc</kbd> there cancels.

- **Complete a name:** press <kbd>Tab</kbd> to complete a name, or, after the
  `@`, the name of a linked BBS; if several fit, they are listed under the
  prompt. Tab only completes names you can write to.
- **Pick from a list:** type `?` and press <kbd>Enter</kbd> for a list of
  everyone you can write to: people you had mail with lately first, then the
  members of this board, then the linked BBSes. Choose a BBS and you are asked
  for the person's user name there; `name@?` lists just the BBSes for that
  name.
- **Who you can't reach:** the list also shows who you can't write to right
  now, dimmed, with `-` instead of a number and the reason beside the name:
  they don't accept your mail, their account is disabled or awaiting
  approval, or their BBS is not linked yet (this board's SysOp hasn't
  finished linking it).

**Several people.** A letter can go to several people, up to 20, here and on
linked BBSes alike: separate them with commas, as in `bob, carol@Farpoint`.

- <kbd>Tab</kbd> completes the name after the last comma, and `?` as the last
  name opens the list for that one.
- Each name is checked as you type it; if one can't be used, you are told
  which and why, and the prompt opens again on what you typed, to fix.
- Each person gets their own copy, and every copy shows everyone it went to
  -- there is no blind copy.
- If one of them can't take it when you press **Send** -- their mailbox is
  full, say -- nothing is sent to anyone: the review screen names them, and
  <kbd>T</kbd> **To** lets you take them off and send to the rest.

#### Send netmail to an FTN network

If this BBS is on an FTN network such as FidoNet or fsxNet, you may be able to
send **netmail** to someone on another BBS of that network. Write their name
and FTN address on the To line, as in `Joe Bloggs (21:3/110)`. The screen
names the network the letter goes out on.

- **Not private:** every system netmail passes through can read it.
- **One person at a time:** a netmail can't go to a list of names.
- **Who may send it:** the SysOp decides who may send netmail. Until they
  choose otherwise, only the SysOp can.
- **Replies:** **Reply** to a netmail in your Inbox, or to one in Sent, writes
  netmail back.
- **How urgently:** on the review screen, <kbd>F</kbd> **Flavour** goes round
  **Normal** (the usual way, through the network), **Crash** (this BBS calls
  the other BBS at once) and **Hold** (it waits for the other BBS; the SysOp
  decides whether it may collect it). If a crash or held netmail can't be
  delivered, the BBS sends you a letter saying so.
- **Files:** a file you point a netmail at reaches the other BBS as its name,
  size and file area, in text. Only the SysOp's netmail sends the file
  itself.
- **No such person:** netmail to a name nobody on the other BBS has may
  come back to you, with the reason.

#### Point a letter at files

A letter can point at files that are already in a file area here, up to
five.

1. On the review screen press <kbd>A</kbd> **Attach file**.
2. Choose the file area and then the file.

<kbd>R</kbd> **Remove file** takes one off again. Nothing is copied: the
letter names the file, and whoever reads it downloads it from the file area.

- Only someone who may open that file area can be sent the letter -- if one
  of your recipients can't, **Send** names them and nothing goes out.
- Someone on another BBS can't download from here: their copy ends with a
  line naming each file, its size, its file area and this BBS, as in
  `File: report.zip (12.3 KiB) in file area "Uploads" on Farpoint`.
- A forward takes the files of the letter it passes on along, and the draft
  of a letter keeps them.

#### Review and send

Nothing is sent or posted until you choose to on the review screen, which
keeps To and Subject at the top and shows a long message a page at a time.

- Turn the pages with <kbd>&lt;</kbd> <kbd>&gt;</kbd> or
  <kbd>PgUp</kbd>/<kbd>PgDn</kbd>.
- <kbd>E</kbd> **Edit body** reopens the text.
- <kbd>B</kbd> **Back** (or <kbd>Esc</kbd>) leaves without sending, after
  asking first if you have written anything.

A post or message needs a subject. Leave it empty and mail asks again; press
<kbd>Esc</kbd> there to cancel the message. A subject that is too long is
caught as soon as you press <kbd>Enter</kbd>, with how many characters to
remove, and the field stays open so you can shorten it.

> [!NOTE]
> If your signature pushes a message over its length limit, the review
> screen says so before you can send it.

## Chat and mail

| At a glance | |
| --- | --- |
| **Where** | <kbd>C</kbd> **Chat** and <kbd>E</kbd> **E-mail** on the main menu |
| **Try first** | Join a chat channel and type `/help` |
| **Good to know** | Mail is NetBBS mail, not an Internet email account |

In this chapter: [Chat](#chat) · [Mail](#mail) ·
[Block someone](#block-someone) · [Bots in chat](#bots-in-chat) ·
[MRC: chat across BBSes](#mrc-chat-across-bbses) ·
[What the SysOp can see](#what-the-sysop-can-see)

### Chat

Choose a chat channel to join the conversation. Type a line and press
<kbd>Enter</kbd> to speak. `/help` lists the commands available there.

| Command | Use |
| --- | --- |
| `/who` | See who's online |
| `/list` | List the channels, with how many are in each, here and on MRC or a linked BBS |
| `/join channel` | Switch to another channel |
| `/leave` | Return to the channel picker |
| `/quit` | Leave chat |
| `/msg user text` | Send a live private message; `/msg Baud.bot text` asks a bot privately |
| `/away text` | Set an away message |
| `/nick name` | Set a chat alias; a bare `/nick` clears it |
| `/dm user` | Invite someone online to a private direct chat; `user@TheirBBS` for someone on a linked BBS |
| `/bell` | Show when chat rings your bell; `/bell off`, `/bell private` or `/bell mentions` sets it |

> [!TIP]
> <kbd>PgUp</kbd> scrolls back through the conversation, a page at a time, and
> <kbd>PgDn</kbd> forward again. While you are scrolled back the chat holds
> still, so you can read or copy at leisure; new lines wait, counted at the
> bottom, until you return with <kbd>Esc</kbd>, with <kbd>PgDn</kbd> past the
> newest line, or by sending a line. Your terminal's own scrollback jumps back
> to the bottom whenever chat redraws, so use these keys instead.

Gray lines with no name in front are the BBS itself speaking: a good-morning
line at midnight, and announcements the SysOp has scheduled, such as
`Announcement: Net meeting tonight at 20:00 in #main.` They stay in the
channel's scrollback, so you see them even if you arrive later. In a channel
shared with linked BBSes, an announcement from another BBS names it:
`Announcement from Vertigo: Tournament at 21:00.`

#### Aliases and names from other BBSes

An alias is always shown with the username beside it, as
`Quill (Copperplate)`, so everyone can see who is speaking: the alias first
and in color, the username after it in gray. An alias is refused when it

- reads like someone else's username, or like a staff title such as "SysOp";
- contains any of `( ) | [ ] < > * ~`.

Someone on a linked BBS is shown as `name@TheirBBS`, with their BBS in a
color of its own. If two BBSes you are linked with share a name, the name
gets its address added, like `alice@Twin BBS · twin.example.org`, so you can
tell them apart.

In a channel shared with linked BBSes, `/who` lists everyone in it on every
one of them, each with the BBS they are on, whichever BBS the channel started
on. A BBS running an older NetBBS version only shows up from the BBS the
channel started on.

#### Direct chats and live messages

A direct-chat invitation opens on your main menu. If you are somewhere else
on the board, a line tells you who invited you; go back to the main menu
within a minute to answer.

You can also chat this way with someone on a linked BBS: `/dm name@TheirBBS`
in chat, or <kbd>I</kbd> **Invite to chat** on their entry in Who's online.
Their invitations to you open the same way, named `name@TheirBBS`.

- If their BBS doesn't support it yet, you are told so; `/msg` still works.
- If they can't be reached, they don't take live messages, or they block you,
  you are only told they can't be reached right now.
- If the connection between the two BBSes drops, the chat ends and says so.

Live private messages need the recipient to be online. **E-mail** keeps a
message for later.

> [!NOTE]
> If live contact fails, send mail explicitly: chat does not silently turn
> it into mail.

#### The chat bell

Chat rings your terminal's bell when it needs you, so you notice from another
window. Choose when in **Profile ▸ Chat bell**, or with `/bell` in chat:

- **Private** (the default): a `/msg` or `/private` message, a message from
  someone on a linked BBS or on MRC, and a `/dm` invitation.
- **Mentions**: those, and a line in a channel that names you, by your
  username or your alias, with or without `@`. Your own lines and bots' lines
  never ring.
- **Off**: never.

Being away doesn't silence it: the bell is for when you are away from the
screen. It rings at most a few times in a row, and once every 30 seconds for
any one person, and never while you are scrolled back with <kbd>PgUp</kbd>.
It rings on the chat screens and in a direct chat; a message that waits for
you elsewhere on the board does not ring.

Your terminal decides whether a bell beeps, flashes or stays quiet. In the
web client it plays a short tone once you have pressed a key or clicked in
the page, and while the tab is in the background its title starts with `(!)`
until you come back.

#### Getting help from the SysOp

To ask the SysOp for help, type `/sysop` and what you need in any chat
channel:

```
/sysop my upload to Retro stops at 90% every time
```

1. Your reason goes to the SysOp and staff, and nowhere else: nothing is said
   in the channel, its history, linked BBSes or MRC.
2. A block that only you see says who was told. Staff in chat are told at
   once; staff elsewhere on the BBS see it on their next screen, or when they
   leave a door.
3. If nobody is online, the page waits for them. When they are all away, the
   block says who is expected back first.

> [!NOTE]
> You can have one page open at a time, and page at most once every 10
> minutes. Paging again after that replaces the reason of your open page. As
> a guest you can page only while someone is online, because a guest's page
> can't be answered later.

The SysOp answers by opening a chat on your screen, or by mail. Use `/sysop`,
not `!sysop`: a `!` line goes to the whole channel before a bot sees it, and
Baud can only point you to `/sysop`.

### Mail

**E-mail** is NetBBS mail, not an Internet email account.

#### Who has mail

Signed in as the board's guest, you have no mail: every guest shares that
account, so its mailbox would be everyone's. Register an account of your own
to send and receive mail.

Mail is refused to:

- the guest account;
- an account that is disabled or still waiting for the SysOp's approval.

The To prompt tells you, and mail from another node bounces with "that
account is not taking mail at the moment". A SysOp can also open mail only
from a certain access level; below it, the main menu offers no **E-mail**.

#### Your Inbox and its limit

**E-mail** opens on your Inbox, with how many messages are unread at the top
and how full it is.

| Inbox | What happens |
| --- | --- |
| Up to 500 messages | The header counts them, for example `120 of 500` |
| From 450 | The Inbox warns you |
| At 500 | Each new message removes your oldest message you have already read; unread mail is never removed |
| Full of unread mail | New mail is turned away until you read or delete some, or move some to **Kept**, which does not count toward the 500 |

If old mail was removed to make room, the main menu tells you how many
messages went, once.

#### Knowing when mail arrives

- The first main menu after you log in says how many messages arrived since
  your last call and how many are unread in all, for example "3 new since
  your last call, 7 unread in all". On your first call it gives just the
  unread count, and with nothing unread it says nothing.
- **New scan** shows the same counts above its list.
- Mail news is shown in green, the colour of good news, not the warning
  colour.
- A message that arrives while you are online is announced with its sender
  and subject: at once in chat, straight away on an idle main menu or Inbox,
  and otherwise above the prompt of the next screen you reach.
- Nothing is written into a door game or an editor while you are in it; the
  notice waits until you leave.

#### Read and manage your mail

Each row shows who a message is from, its subject and its date, with `new`
beside mail you have not opened yet. Move with <kbd>Up</kbd>/<kbd>Down</kbd>
and press <kbd>Enter</kbd>, or press a row's number, to read a message. On a
long list, <kbd>&lt;</kbd> and <kbd>&gt;</kbd> (or <kbd>PgUp</kbd> and
<kbd>PgDn</kbd>) turn the page. The other keys:

| Key | What it does |
| --- | --- |
| <kbd>S</kbd> **Sent** | Lists the mail you have sent. <kbd>B</kbd> **Back** there returns to the Inbox. |
| <kbd>V</kbd> **View Kept** | Lists the letters you keep (see [Keep letters](#keep-letters)). |
| <kbd>C</kbd> **Compose** | Writes a new message. |
| <kbd>M</kbd> **Mark** (or <kbd>Space</kbd>) | Marks the highlighted message with `*` and moves to the next, so you can mark a run of them; press it again on a marked one to unmark it. |
| <kbd>E</kbd> **Erase** | Deletes the marked messages, after one question that says how many; with nothing marked it deletes the highlighted one. |
| <kbd>P</kbd> **Purge read** | In the Inbox, deletes every message you have read, after one question that says how many. Unread and kept mail stays. |
| <kbd>O</kbd> **Order** | Switches between newest first, unread first, and by conversation. In Sent it switches between newest first and by conversation. Your choice is remembered. |
| <kbd>/</kbd> (**Find**) | Shows only mail with a word in the name or the subject. An empty line shows everything again. |
| <kbd>U</kbd> **Unread** | Marks the highlighted message unread, or read without opening it. While you read a message, its own <kbd>U</kbd> **Unread: no** marks it unread again. |

By conversation groups the letters with one person under one subject (`Re:`
and `Fwd:` aside), the conversation with the newest letter first and its
earlier letters indented under it.

A `!` before a sender's name means their node's identity has changed. The
message explains it when you open it.

While you read a letter, <kbd>D</kbd> **Decode ROT-13: off** shows a
letter written in ROT-13 decoded, as on a board (see
[Read and reply](#read-and-reply)). The letter itself does not change.

#### Keep letters

<kbd>K</kbd> **Kept: no** in the Inbox (on the list or in a letter) moves a
letter to Kept, and <kbd>K</kbd> **Kept: yes** in Kept moves it back. In the
Inbox, **Kept** moves the marked messages to Kept the same way.

- The mailbox never removes a kept letter to make room, and kept letters do
  not count toward the Inbox's 500.
- Kept has its own limit instead: it holds 100 letters, and its header counts
  them, for example `40 of 100`.
- When it is full, keeping another letter is refused with "Kept is full (100
  letters) -- move some back to the Inbox or delete them first."
- Keeping marked letters is all or none: if they do not all fit, none move,
  your marks stay, and you are told how many more Kept has room for.
- The Inbox header says how many are in Kept.

#### Write to someone from where you found them

You can write to someone without typing their address. The compose screen
opens with **To** filled in:

| From | Press |
| --- | --- |
| The **Directory**: open a member's card | <kbd>M</kbd> **Mail** |
| **Who's online**: pick a caller | <kbd>E</kbd> **E-mail** (<kbd>M</kbd> **Message** there is a live message). It works for a caller on a linked node, and for a caller who has turned off live messages. |
| **Recent callers** | <kbd>M</kbd> **Mail a caller**, then type the caller's number. A caller whose name is hidden there can't be written to from the list. |
| Reading a post | <kbd>M</kbd> **Mail author** answers its author privately, with the post's `Re:` subject and a quote, as a reply on the board would have. You don't need to be allowed to post on that board, and it works for a post carried from a linked node too. |

After you send the letter, keep it or cancel it, you are back on the screen
you came from, with the result above the prompt.

- None of these is offered for yourself, or while mail is closed to you.
- None of these offers mail to someone on this board who has blocked you.
  The member card and Who's online say that they do not accept messages or
  mail from you, Recent callers says so when you type their number, and a
  post of theirs has no **Mail author**.
- For someone on a linked node you are still offered mail, since this board
  can't see who they block; their board answers.
- If the person can't be written to, for example the guest account or
  someone on a node that has only just linked with this one, you are told why
  when you press the key.

#### Forward a letter

1. Open it, in the Inbox or in Sent.
2. Press <kbd>F</kbd> **Forward**.
3. Type whom it is for at the To prompt, a name here or an address on a
   linked node.

The subject gets `Fwd:` in front, and the letter goes whole under a short
header saying whom it was from and to, when, and under what subject. What you
type goes above it, as a note, and your signature goes under the note; in the
line editor, `/end` writes at the end instead.

A letter already at the size limit is too long to forward as it is: the
review screen says by how much, and <kbd>E</kbd> **Edit body** lets you
shorten it. Mail from **System** can be forwarded too.

#### Mail from System

Mail from **System** was sent by the BBS itself, not by a person -- for
example, when a moderator turns down a post you wrote, with their reason and
your text. There is no one to reply to, so it has no **Reply**; to question
the decision, write to the moderator or the SysOp.

#### Mail to other nodes

On a linked node, addresses can use a known, unambiguous name such as
`alice@OtherNode`.

- Use the address offered by the directory or Who screen, or the one on the
  From line of their mail. Type it exactly as shown, capitals included.
- A node name that itself contains `@` is shown in quotes, like
  `bob@"Cats @ Night"`; type the quotes too.
- Mail checks the address as you type it and asks again if it can't be used.
  If more than one linked node goes by the name you typed, it lists the exact
  address to type for each.
- A node that has only just linked with this one cannot be written to yet:
  the To prompt says so, and mail opens once your SysOp establishes that
  node.

Check an unexpected node-identity warning with your SysOp.

> [!NOTE]
> Ordinary NetBBS Link mail is protected between nodes but does not hide its
> contents from the home-node operators.

<kbd>R</kbd> **Reply** works on mail from another node too: the reply goes
back to the address on its From line, with the same `Re:` subject and quote a
local reply gets. If that node can't be written to any more, Reply says why
instead of opening the message.

**Sent** shows each message's recipient, with the full address for mail that
went to another node. Your copy stays in Sent even if the recipient's account
is later deleted; it then reads, for example, `bob (deleted account)`.

#### Where a letter to another node stands

Mail to another node also shows where it stands, in Sent's list and on its
Delivery line when you open it:

| State | Meaning |
| --- | --- |
| **Pending** | On its way; the other node has not confirmed it yet. |
| **With relay** | The other node can't be reached directly, so your BBS left the letter at a relay for it to collect, and no answer has come back yet. If none comes within 14 days, it expires. |
| **Delivered** | It is in the recipient's mailbox. |
| **Bounced** | The other node sent it back, and the Delivery line says why, for example that there is no user by that name there, that their mailbox is full of unread mail, or that the node does not trust yours yet. |
| **Expired** | No route to that node worked before delivery gave up, or this BBS's SysOp stopped trying to send it, so it was not delivered. Mail left at a relay expires when no answer came back in 14 days; it may still have arrived, and if an answer turns up later, Sent shows it as delivered or bounced after all. |
| **Resent** | It bounced or expired, and you sent it again with **Send again**. The new copy is a letter of its own in Sent, with its own state. |

#### Send a letter again

When mail bounces or expires you are told at the main menu, once, even if it
happened while you were away. Opening the message in Sent counts as being
told.

1. Open it in Sent and press <kbd>S</kbd> (**Send again**).
2. The compose screen opens with the same recipient, subject and text, ready
   to send as a new letter.
3. Once it is sent you are back on the Sent list, and the old letter reads
   **resent** there. Opening it shows when, on a `Resent:` line under its
   Delivery line, and the key reads **Send another copy**.

- If you sent the resend to someone else instead, the old letter stays as it
  was.
- If the reason it failed still applies on this side -- for example your
  SysOp has since stopped mail to that node -- you are told so when you press
  the key or at Send.
- Resend is only offered for mail that bounced or expired: a letter that
  arrived, or may still arrive, would reach its reader twice.

<kbd>R</kbd> **Reply** and <kbd>F</kbd> **Forward** on a letter in Sent also
bring you back to the Sent list once the new letter is sent.

#### Follow up on a letter you sent

Open it in Sent and press <kbd>R</kbd> **Reply**. It writes to the letter's
recipient, with `Re:` in front of the subject and your letter quoted, as a
reply to mail you received would. You can't reply to a letter whose
recipient's account has been deleted.

Whichever key on a letter in Sent sends a new letter, you are back on the
Sent list, with the new letter at the top and the result above the
prompt (or on Find's results, if you opened the letter from there).

#### Letters to several people

A letter to several people is one row in Sent, with everyone's names under
To. Opening it shows a Delivery line for each person on another node, and the
list shows the one that needs you most -- a bounce before a letter still on
its way.

- **Send again** there sends the letter again only to those whose copy
  bounced or expired and was not resent yet. The list reads **resent** once
  every one of them was, and the key then reads **Send another copy**.
- <kbd>R</kbd> **Reply** writes to them all again.
- <kbd>E</kbd> **Erase** removes it from Sent for everyone it went to (their
  copies stay theirs).

When you receive such a letter, its To line shows everyone it went to, and
<kbd>A</kbd> **Answer all** writes to its sender and everyone else on it,
where <kbd>R</kbd> **Reply** writes to the sender alone. Anyone who can't be
written to any more is left out, and you are told who.

#### How a letter looks

A letter shows the way its writer typed it: each line stays a line, and only
a line too wide for your screen wraps. Mail can be in color, written with pipe
codes like `|12` or pasted in, as on a board that allows color. Profile's
**Colors in posts and mail** switch covers mail too; with it off, colored mail
reads as plain text.

#### Files in a letter

A letter that points at files lists them under its Date line: each file's
name, size and file area. Press <kbd>G</kbd> **Get file** to download one --
with several, you choose which -- the same way the file area downloads it: by
Zmodem, or with a browser link where your terminal has no Zmodem or the Zmodem
transfer fails.

- A file deleted or expired since the letter was sent, or taken out of its
  file area, shows as no longer available.
- A file in a file area you can't open, for example because its access level
  was raised after the letter was sent, shows only as a file in a file area
  you can't open.

#### Mail to everyone

Mail your SysOp sends to all callers shows `Everyone on this BBS` as its To.
If it comes from **System** it has no Reply; if it comes from the SysOp's own
account, <kbd>R</kbd> **Reply** answers the SysOp.

#### Read receipts

When you send mail to someone on this board, Sent shows whether they have
read it:

- The list says `read` or `not read`, and the opened letter has a `Read:`
  line with the time they first opened it (marking it unread again does not
  take that back).
- A letter to several people lists who has read it and who hasn't yet.
- A letter deleted without being opened simply reads as not read: nobody
  learns that you deleted it.

The same goes the other way: whoever writes to you sees when you first open
their letter.

This is on for everyone unless they turn it off, and it works both ways. In
**Profile ▸ Let senders see when I've read their mail** you can turn it
off: then no one sees when you read their mail, and you no longer see when
anyone reads yours. Turning it off also hides the receipts you already gave;
turning it on again shows them again.

> [!NOTE]
> A letter opened while either of you had receipts off never shows as read,
> even once you both turn them back on -- so nobody can switch receipts on
> for a moment to see what you read while they were off, and what you read
> while yours were off stays unreported.

A sender is told that you don't share read receipts (`no receipt` in their
Sent list), so an unread-looking letter isn't taken for one you haven't read
-- they learn your setting, never whether you read it.

- Mail to or from another BBS, and mail from **System**, never has read
  receipts.
- For mail your SysOp sends to all callers, they see only how many of those
  who share receipts have read it.

### Block someone

To stop someone's mail and live messages:

- open a letter from them and press <kbd>S</kbd> (**Sender blocked: no**), or
- pick them on **Who's online** and press the same key there.

It then reads **Sender blocked: yes**, and pressing it again takes the block
back. It works for someone on this board and for someone on a linked node,
who is blocked by their address there, so a node changing its name does not
undo it.

**Profile ▸ Blocked people** lists everyone you block, blocks someone by
name (`alice`, or `alice@OtherNode`) before they have written, and unblocks
with <kbd>U</kbd> **Unblock** or by picking a row. Mail already in your Inbox
stays there.

#### What a block stops

One block stops both their mail and their live messages:

- `/msg`, `/private` and `/dm` invitations;
- messages and chat invitations from Who's online;
- inviting you into a chat channel with `/invite`.

If they are in a private conversation with you when you block them, it ends.

#### A blocked person is told

- On this board the To prompt and Send say that you do not accept mail from
  them, and `/msg`, `/private`, `/dm` and `/invite` say you do not accept
  messages from them.
- Who's online and your Directory card say you do not accept messages or
  mail from them, and offer neither; Recent callers says it when they type
  your number, and your posts offer them no **Mail author**.
- Mail from another node bounces with that reason. It is never taken and
  quietly thrown away.
- The one exception is a live message from someone on another node: it is
  dropped without an answer, because live messages between nodes have no way
  to send one back.

The same works the other way: if you are told someone does not accept mail or
messages from you, they have blocked you.

Two senders cannot be blocked: **System**, and this board's SysOp, who has to
be able to reach every account on the board.

> [!TIP]
> A block does not hide what someone says in a chat channel everyone shares;
> ask a channel operator or the SysOp if someone is disrupting a channel. To
> stop live messages from everyone at once, turn off **Direct messages** in
> Profile; it applies to `/msg` and `/private` as well as `/dm` and Who's
> online, and does not stop mail or channel invitations.

### Bots in chat

Bot messages are marked `[BOT]`, including when replayed from chat history.
`/bots` shows your node's policy for the current channel. Its moderators can
choose all bots, only this node's bots, or none; the choice also applies to
Find and remote bot entries in `/who` and `/names`. `/who` and `/names` show
which bots are running here, and which bots read the whole channel.

#### Talk to Baud

If your SysOp has enabled Baud in a local channel, type `Baud.bot: help`.
When it is the channel's default bot, the short `!` forms work too; without a
default, prefix the commands with `Baud.bot:` instead of `!`.

| Command | Baud is the channel's default bot | No default bot |
| --- | --- | --- |
| Help | `!help` | `Baud.bot: help` |
| Set a reminder | `!remind 10m check the oven` | `Baud.bot: remind 10m check the oven` |
| Set a reminder for a time | `!remind fri 20:00 net meeting` | `Baud.bot: remind fri 20:00 net meeting` |
| List reminders | `!reminders` | `Baud.bot: reminders` |
| Cancel a reminder | `!cancel 1` | `Baud.bot: cancel 1` |
| Leave someone a note | `!tell Nib check the Retro board` | `Baud.bot: tell Nib check the Retro board` |
| List your notes | `!tells` | `Baud.bot: tells` |
| Cancel a note | `!cancel tell 4` | `Baud.bot: cancel tell 4` |
| Roll dice | `!roll 2d6+1` | `Baud.bot: roll 2d6+1` |
| Flip a coin | `!coin` | `Baud.bot: coin` |

To reach the SysOp, use `/sysop <reason>` rather than Baud: see
[Getting help from the SysOp](#getting-help-from-the-sysop).

- Baud answers `help` and `reminders` privately, where only you see the
  answer; `reminders` then lists your reminders in every channel, and
  `cancel` works on any of them from where you are. Its other
  replies to a line typed in the channel are public there. To keep a question
  and its answer to yourself, [ask privately](#ask-a-bot-privately).
- A reminder is said publicly in its channel when it is due.
- A longer answer, such as `help`, comes as a titled block, its rows lined
  up the way `/who` lines up its own, and `help` colours its commands the way
  `/help` does:

  ```
  ── [BOT] Baud.bot · Commands ───────── to you · 20:14 ──
   Happy to help!
   help                  this list; help remind or help tell for more
   remind <when> <text>  a reminder here, shown publicly
  ```

  `[BOT] Baud.bot` is put there by your BBS, so a block always says which bot
  it is from; `to you` marks one only you see. One taller than your screen
  is shown a page at a time: <kbd>PgDn</kbd> (or <kbd>Enter</kbd> on an empty line) for the next,
  <kbd>Esc</kbd> to skip the rest.
- Reminders survive restarts, though mutes or limits can delay them.
- A reminder takes a duration (`10m`, `2h`, up to `30d`) or a time on this
  board's clock: `20:00`, `tomorrow 09:00`, `mon` to `sun` with a time, or a
  date as `2026-11-01 20:00`. Times are 24-hour. A time already past today
  means tomorrow. Baud's answer names the moment, for example
  `Saved reminder #3 for Fri 20:00 CET.`
- If you are not in the channel when a reminder falls due, Baud keeps it for
  a week and says it when you next join that channel, marked
  `held until you came back`. `!reminders` shows it as `waiting since 18:00`
  until then; after a week it is dropped without a word.
- `!tell` leaves a note of up to 160 characters for an account on this board.
  Baud says it publicly in the same channel the next time they join it, within
  14 days, as `Nib: Quill asked me to tell you (3h ago): ...`. If they are in
  the channel already, Baud says so and keeps nothing. You can have three
  notes waiting, and one person five.

> [!NOTE]
> Turning off **Direct messages** in Profile, or blocking someone, also stops
> their notes through Baud: a new one is refused, and one already waiting is
> quietly dropped instead of being said.
- Ordinary conversation is not sent to an addressed-only bot.
- Depending on what your SysOp allows, Baud may also answer `stats`,
  `seen <account>` and `top <game>` (a game's top ten, see
  [Game results in chat](#game-results-in-chat)), greet people who join, or
  announce new posts, files and game results; its `help` lists what it offers
  here.
- In a Linked channel, `Baud.bot@Node: help` asks a bot on another node.

> [!NOTE]
> If your SysOp has enabled model conversation, other text addressed to Baud
> goes to a language model: your first such message only shows a notice
> naming the service, the replies are public unless you
> [asked privately](#ask-a-bot-privately), and `reset` clears your
> conversation.

As a help desk, which is how most nodes set it up, Baud answers questions
about using the BBS, such as `Baud.bot: how do I upload a file?`. It answers
from parts of this handbook and from facts about the node, and names the
section it used: `(Handbook: Exchange files. May be wrong; ? for help)`.
Asked privately, with `/msg Baud.bot` or in `/private Baud.bot`, it can also
name the boards and file areas you can see; in the channel it names only the
ones everyone can read. A model can be wrong, so check what it says against
**Help** (<kbd>?</kbd>) on the main menu or ask the SysOp.

#### Dice, quotes and quick polls

`!roll` rolls one six-sided die; `!roll 3d20`, `!roll d8` and `!roll 2d6+1`
roll up to 20 dice of 2 to 1,000 sides, with a bonus or penalty of up to
1,000. Baud says each die and the total, so everyone in the channel sees the
same roll. `!coin` flips a coin.

If your SysOp has switched them on, Baud also keeps quotes and runs quick
polls in the channel:

| To | Type |
| --- | --- |
| Save a quote | `!quote add <Quill> the modem is on fire again` |
| Show a random quote, or quote 4 | `!quote`, `!quote 4` |
| Delete quote 4 | `!quote del 4` |
| Open a poll for 10 minutes | `!poll Best door? \| Voidrunner \| War Dialer` |
| Open one for an hour | `!poll 1h Best door? \| Voidrunner \| War Dialer` |
| Vote for answer 2, or change your vote | `!vote 2` |
| See the question and how many voted | `!poll` |
| Close your poll early | `!poll close` |

- A quote shows who added it, `(added by Nib)`, and stays in its channel. You
  can delete the quotes you added; a channel moderator can delete any.
- A poll has 2 to 6 answers and lasts up to 24 hours. Baud says the results
  once, when it closes. Your `!vote` line is public chat, and Baud confirms
  it to you alone.
- In a Linked channel, a poll opened on another BBS takes that BBS's address:
  type what its opening line shows, for example
  `Baud.bot@Roanoke: vote 2`. A plain `!vote` asks your own BBS's Baud, which
  tells you so.
- Guests can vote, but cannot add quotes or open polls.

#### Ask a bot privately

A question typed into the channel is seen by everyone there. To ask a bot
where only you see the question and its answer:

| To | Type |
| --- | --- |
| Ask one question | `/msg Baud.bot reminders` |
| Keep asking | `/private Baud.bot`, then type your questions; `/close` ends it |
| Ask a bot on a linked BBS | `/msg Baud.bot@Node help` or `/private Baud.bot@Node` |

- <kbd>Tab</kbd> after `/msg ` or `/private ` completes a bot's name as well
  as a person's. It offers the people online first, then the bots that can
  answer you in this channel. A bot on a linked BBS completes as
  `Baud.bot@Node`.
- What you asked shows as `(privately to Baud.bot: ...)`, and the answer as a
  bot line marked **(to you)**:
  `[20:14] [BOT] <Baud.bot> (to you) Saved reminder #4.`
  A private titled block says **to you** at the right of its title.
- Nobody else sees either, and neither is kept in the channel's history or
  search. Like `/msg`, the answer stays among your own lines, so
  <kbd>PgUp</kbd> shows it, and it never rings your bell.
- A bot may also answer a question you asked in the channel this way.
- The bot has to be allowed in the channel you are in. If it is muted there,
  or the channel's `/bots` setting hides it, you are told why.
- A bot answers each caller at most 30 times an hour privately; when that
  runs out, you are told so.
- A bot on another node can answer privately only if that BBS, and yours,
  support it; otherwise you are told to ask in the channel instead.
- If you leave the channel before the answer arrives, it is not shown.

### MRC: chat across BBSes

Channels marked **MRC** connect to a separate public chat network.

> [!NOTE]
> Your handle and messages are visible on that network. MRC private messages
> are optional in your profile; they are not confidential from that network.

- Use `/mrc` for its help and commands.
- In the **Multi Relay Chat** section of **Chat**, start with `lobby` to
  discover network rooms.
- `/rooms` refreshes the list; return to the picker to choose a room with its
  user count and topic, or use `/join room`.
- <kbd>Tab</kbd> completes MRC subcommands, room names and recipients for
  `/mrc msg`.
- If opening a room is refused, the reason stays visible in the picker while
  you choose another room or go back.

A line from MRC starts with **[MRC]**, and its sender reads `nick@theirBBS`,
with their BBS in a color of its own.

The network's answer to an `/mrc` question, such as `/rooms` or `/mrc bbses`,
appears a moment after it finishes arriving, as one block headed
`MRC hub · /mrc bbses`. A long answer comes a page at a time: press
<kbd>PgDn</kbd> (or <kbd>Enter</kbd> on an empty line) for the next page and
<kbd>Esc</kbd> to skip the rest. Long command replies, like `/list` on a board
with many channels, page the same way.

#### Stylized MRC names

Some MRC users' software puts a styled name in front of their lines, such as
`+Nick+[CASTLE BBS]`. Profile's **Stylized MRC names** chooses how you see it:

| Setting | Shows |
| --- | --- |
| **combined** (the default) | `<+Nick+@Castle_BBS (CASTLE BBS)>`, the styled name in place of the plain one and the rest in brackets |
| **both** | `<Nick@Castle_BBS> +Nick+[CASTLE BBS]`, the styled name as sent |
| **label only** | `<Nick@Castle_BBS>` |

The BBS name always comes from the network, never from the styled name. Lines
that arrived before v7.18.0 look the same in all three.

#### The MRC status bar and colors

The status bar separates people **here** (including their away count) from
people on **MRC**. Remote away counts are unavailable; `?` means the roster
has not arrived, and stale readings are labeled. Profile's
**Inter-BBS chat (MRC) colors** switch controls incoming body and nickname
colors, including scrollback.

### What the SysOp can see

As on most BBSes, the SysOp of the node you are connected to can watch your
session live:

- They see your screen as you see it, including chat and private messages,
  and what you type where it shows on screen.
- Passwords are not shown as you type them, so they are never visible this
  way.
- You are not told while it happens, but the node keeps a log of every time a
  SysOp watches a session. The last line of chat's `/help` says the same.

The SysOp can also break into your session for a live chat. Your screen turns
into a two-part chat window: the SysOp types in the top half and you type in
the bottom half.

- When the SysOp ends the chat, your screen comes back exactly as you left
  it, with any half-typed line still there.
- If you were in a game, the game kept running during the chat.
- A break-in can't start while you are typing a password, and anything typed
  at a password prompt shows as `*` in the chat.

## Exchange files

| At a glance | |
| --- | --- |
| **Where** | <kbd>F</kbd> **Files** on the main menu, then a file area |
| **Try first** | <kbd>D</kbd> **Download** on a file that interests you |
| **You need** | A Zmodem terminal such as SyncTERM, or the browser |

Open a file area and select an upload or download action:
<kbd>D</kbd> **Download** for the highlighted file, <kbd>U</kbd> **Upload** to
send one. There are three ways to move the file:

- **Browser transfer controls**, in the browser terminal.
- **A browser link** offered by the node. Browser links require the SysOp to
  configure the web listener.
- **Zmodem**, with a Zmodem-capable terminal client. PuTTY and ordinary SSH
  clients do not provide Zmodem by themselves.

### Zmodem

A Zmodem terminal such as SyncTERM starts its side of the transfer by itself:
it saves a download, or asks which file to upload. If yours doesn't, start its
Zmodem receive or send by hand.

- Press <kbd>Ctrl</kbd>+<kbd>X</kbd> five times to cancel a transfer.
- If a Zmodem transfer fails, you stay on the file list: press
  <kbd>W</kbd> **Web transfer** there for a browser link instead.
- After a Zmodem upload you are back on the file list too, with the cursor on
  the new file, so <kbd>E</kbd> **Edit description** describes it straight
  away.

### Browser links

When you upload through a browser link, the terminal tells you once the file
has arrived, and whether it waits for approval before others can see it. The
message names the file area the file went to in its own color. In the browser
terminal the list updates by itself; elsewhere the list shows the new file the
next time it is drawn, for example when you press <kbd>Ctrl</kbd>+<kbd>L</kbd>.

> [!NOTE]
> Treat a transfer link like a password: it grants access to that transfer.
> Links expire; request a fresh one if needed.

A remote file may first need to be fetched from its originating node. If that
node is unavailable, try later or tell your SysOp.

## Play a door game

| At a glance | |
| --- | --- |
| **Where** | <kbd>G</kbd> **Games** on the main menu, or <kbd>G</kbd> **Games** in a Community under <kbd>T</kbd> **Topics** |
| **Try first** | Retro Trivia, if your SysOp offers it: one short round |
| **Leave** | Through the game's own menu |

Availability depends on what your SysOp has registered. The bundled games:

| Game | What you do |
| --- | --- |
| **Retro Trivia** | Answer a short round of multiple-choice questions. |
| **Voidrunner** | Build a persistent space-trading and exploration career. |
| **War Dialer** | Develop a crew in a shared world of fictional BBS networks. |
| **Lunar Lander** | Fly a lander down onto a lit pad, and chase the best landing on each mission. Needs a terminal of at least 80 by 24. |

Each game opens with a short animated title screen. Any key skips it; once it
has finished, it stays up until you press a key. Voidrunner and War Dialer
leave it out when their display settings turn motion off.

The games also play short animated scenes during play: Voidrunner at jumps,
docking, fights and milestones, War Dialer after raids, jobs and busts, and
Retro Trivia at the end of a round. A scene shows a result that has already been
saved. Any key ends it, and that key is used up, so it can't choose anything on
the next screen. Voidrunner also keeps achievements for your account, across
every career: open Pilot Record in the game and press <kbd>A</kbd>.

- Each game has its own controls and help.
- Quit through the game's own menu to return to NetBBS.
- A busy game may have a session limit; try again later.

> [!TIP]
> Your SysOp controls the time allowed and can help with a missing or locked
> save.

### Game results in chat

Your SysOp may let Baud announce what players earn in a game -- a new rank, a
top achievement, a season's medals, a captured exchange, a perfect round -- in
a chat channel, and answer `!top <game>` with the game's top ten:

```
[20:14] [BOT] Baud.bot: Nib reached Merchant Captain in Voidrunner.
```

Only what you earn while playing is announced, never your earlier play.
**Profile ▸ Game results announced in chat** decides whether Baud may name you:

1. Open <kbd>P</kbd> **Profile** and choose **Game results announced in chat**.
2. Pick **no**. None of your results is announced from then on, and `!top`
   leaves you out. Pick **yes** to be included again.

> [!NOTE]
> It is on by default. The games themselves, and their own Halls of Fame, are
> not affected.

## Preferences and help

| At a glance | |
| --- | --- |
| **Where** | <kbd>P</kbd> **Profile** on the main menu |
| **Try first** | **Profile ▸ Character set**, if lines or arrows look wrong |
| **Finish** | <kbd>L</kbd> **Logoff** on the main menu |

In this chapter: [Your terminal](#your-terminal) ·
[Banners](#banners) · [Calling as a guest](#calling-as-a-guest) ·
[Access requirements](#access-requirements) ·
[Verified age and name](#verified-age-and-name) ·
[Logging off and reporting problems](#logging-off-and-reporting-problems)

Use <kbd>P</kbd> **Profile** for display, editor, and chat preferences, your
SSH keys, and your password.

### Your terminal

**Profile ▸ Character set** sets the characters NetBBS sends your terminal:

| Setting | Suits |
| --- | --- |
| **Auto** | The default. Follows what your terminal reports when you connect. |
| **Unicode** | Modern terminals and the browser. |
| **CP437** | Classic BBS terminals such as SyncTERM. Shows ANSI art as it was drawn. |
| **ASCII** | Plain text that any terminal shows. |

If your terminal did not say which it is, NetBBS asks once after you log in
which of two sample lines looks right.

**Profile ▸ Color depth** sets how many colors NetBBS sends:

| Setting | Suits |
| --- | --- |
| **auto** | The default. Follows what your terminal reports when you connect. A classic BBS terminal, or one that reports nothing, gets 16 colors. |
| **truecolor** | Terminals that show full color, such as SyncTERM and most modern terminals. |
| **256** | Terminals that show the 256-color palette. |
| **16** | Classic terminals that show only the sixteen ANSI colors. |

- If lines and arrows look wrong later, change **Character set**.
- If text blinks, shows reversed or looks garbled where colors should be,
  set **Color depth** to 16.
- If you see only a few plain colors on a modern terminal, set **Color
  depth** to 256 or truecolor.
- <kbd>Ctrl</kbd>+<kbd>L</kbd> redraws ordinary NetBBS screens after a display
  problem. Games may use different controls.

<details>
<summary>SyncTERM's keys</summary>

SyncTERM's Home, End, Page Up, Page Down, Insert and Delete keys
work as they do in other terminals. SyncTERM's Backspace key sends the same
code as <kbd>Ctrl</kbd>+<kbd>H</kbd>, so in SyncTERM it stays Backspace, and
<kbd>F1</kbd> opens a screen's help instead. NetBBS names F1 where other
terminals see Ctrl-H.

</details>

### Banners

Some nodes draw their welcome art or main-menu art slowly, at the speed of an
old modem. Press any key to draw the rest at once; that key is used up, so it
doesn't also act at the next prompt.

**Profile ▸ Banner animations** set to quick draws every banner after you sign
in at once. The welcome banner comes before you sign in, so it plays at the
node's speed whatever you chose; any key still draws the rest.

### Calling as a guest

Signed in as the board's guest, you share one account with every other guest.

**What stays the same for everyone.** Profile won't change anything other
callers see of the guest account:

- the bio, signature, name and details;
- who it blocks;
- whether it takes messages;
- its MRC settings.

Each of those says so when you press it, and so do `/nick` and
`/mrc register`, `identify` or `send` in chat, and **Sender blocked**
(<kbd>S</kbd>) in <kbd>W</kbd> **Who's online**.

**What changes for this call only.** Display settings still change: character
set, color depth, redraw style, banners, editor, colors and sort orders. They
go back to the account's own the next time anyone signs in as the guest.

**What you leave behind.**

- Your unfinished posts and file descriptions are kept only until you hang up.
- You can edit, withdraw or describe only what you wrote during this call;
  posts other guests wrote under the account can't be changed.
- The bundled games play normally, but your progress lasts for the call only,
  and your scores and callsign don't reach the Hall of Fame, the War Dialer
  world or Lunar Lander's best pilots.

> [!TIP]
> Register an account of your own to keep your settings.

### Access requirements

Some resources require an account level, verified age, or verified name. When
you open one, a **Requires:** line under its title says what it asks, for
example `Requires: age 18+ verified · level 20+ to post`. A requirement you
don't meet is shown in red, or followed by `(not met)` if you use plain ASCII,
so the same line tells you why you can read a board but not post.

A name requirement may also disclose the verified name beside contributions in
that resource. Ask the SysOp what is required before sharing identity
information. You cannot grant yourself access by editing your profile.

What you write there keeps the name it was shown under when you wrote it: your
display name, and your verified name if you had one then. A post, an upload or
a chat line from before your name was verified stays without it, and one
written after keeps it even if your display name or verified name changes
later, or the verification is revoked. An edit keeps the post's original
name.

### Verified age and name

An entry marked **needs verification** asks for a verified name, or for a
verified age when the birthdate in your profile is old enough but nobody has
verified it. Opening it tells you which. The SysOp, or someone your SysOp
allows to verify identity, can verify it for you.

Your birthdate goes in **Your profile ▸ Name & details**. That screen shows
what this BBS verified ("Verified by this node: born …") apart from what you
entered yourself.

- Your display name, location and birthdate open for editing with the current
  value in the line: <kbd>Enter</kbd> saves, an empty line clears it, and
  <kbd>Esc</kbd> leaves it as it was.
- A verified value stays until whoever verified it revokes it.

**Sharing over Link.** A SysOp-verified age or name stays on this BBS unless
you switch on sharing over Link for it under **Profile** (**Share verified age
over Link**, **Share verified name over Link**).

- Shared, it goes only to the other BBSes your SysOp has chosen, and the number
  shown beside "on" is how many that is now.
- Switching it off makes this BBS stop giving it out and tells those BBSes to
  forget it. One that already copied it cannot be forced to.

### Logging off and reporting problems

To finish, return to the main menu and choose <kbd>L</kbd> **Logoff**. When
reporting a problem, tell the SysOp which screen, connection method, and
action caused it.

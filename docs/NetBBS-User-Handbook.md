# NetBBS User Handbook

NetBBS is a text-based community with message boards, chat, mail, files,
and games. Each BBS is run by a **SysOp**: the person who manages that node,
sets its rules, and can help with your account.

[All documentation](README.md) · [Running a node](NetBBS-SysOp-Handbook.md)

## Connect and sign in

Use the web address or terminal connection details your SysOp provides.
A browser needs no terminal software. An SSH connection looks like
`ssh -p 2222 yourname@bbs.example.org`; substitute the actual port and address.
Use SSH or HTTPS when available: plain Telnet does not encrypt your login.

Log in with your account. If registration is offered, type `new` at the
username prompt (over SSH, connect as `new`). Usernames use the letters A-Z
without accents, digits, `_`, `-` and `.`, up to 32 characters; the name is
checked before you choose a password. Some nodes require approval before you
can log in: until the SysOp approves your account, logging in tells you it is
still waiting, and you cannot look around in the meantime. If registration is
closed, contact the SysOp. Accounts and access rules belong to each node.

Change your password under **Profile → Account password**: you type the
current one, then the new one twice, and nothing is echoed. If you have
forgotten it, ask the SysOp to set a new one; nobody can recover the old one.

## Find your way around

Press a highlighted letter such as **[M]** for **Message boards**. Most menus react
immediately, without Enter. In the browser you can also click an entry or a
numbered row. In a list, type a row's number: two digits (**03**) or one digit
and Enter (**3** Enter). **[?] Help** on the main menu sums up how the board
works and who runs it. To write to the SysOp, send E-mail **To: sysop**.
When typing text, use Enter to submit it.
**Ctrl+U** clears the line you are typing, and at a field that allows it,
Esc leaves it without changing anything.
Follow the action bar on the current screen: letters can mean different
things in different places. **[B]ack** leaves most screens and goes one level
up: from a board to the list you picked it from, and from there to the menu.

| Main-menu choice | What it does |
| --- | --- |
| **Message boards** | Every message board on this node |
| **Chat** | Every chat channel |
| **Files** | Every file area |
| **Games** | Door games, when the SysOp has set some up |
| **Communities** (key **O**) | Topics the SysOp set up, each with its own boards, chat channels, file areas and games |
| **New scan** | See unread activity and resources you have not visited; Back from each one brings you back with the next new one ready for Enter, and **[R]eplies** opens the replies to your posts |
| **Find** (key **/**) | Search posts, files and retained chat on this node. It does not search mail; the mailbox has its own **[F]ind** |
| **E-mail** | Read and send persistent NetBBS mail, from the level the SysOp chose; not offered to the guest account |
| **Who's online** | See callers and available ways to contact them |
| **Profile** | Change your public profile and personal preferences |
| **Moderation** (key **A**) | Posts and uploads waiting for your decision, if the SysOp made you a moderator |
| **Staff** | Your staff console, if the SysOp gave you staff permissions |
| **Staff list** (key **T**) | Who runs this node: the SysOps, staff and moderators, when each was last on, and who is away |

Lists of boards and file areas keep the SysOp's order, so the number you
remember for a board still picks it next time. **[O]rder** in a list sorts it
another way, by activity, name, newest or size, just this once or from then
on; **[S]ysOp's order** there puts it back.

Some choices appear only when they have something to show or you have
permission to use them. Search covers content held by this node, including
carried NetBBS Link content; it is not a search of the entire network.

## Read and post

Open a message board, select a post, and use the displayed actions to read,
reply, or start a topic. Follow a resource to make it easier to revisit.
On a moderated board, your post may wait for approval before others see it.
You are told at the main menu when it is approved or rejected; a rejection
also arrives as mail from **System**, with the reason and what you wrote.

The text editor shows its commands. In the line editor, a blank line starts
a new paragraph; press Enter on an empty line twice, or type `/done`, to
finish. `/insert N` writes the next lines before line N until you type
`/end`, which is how you answer between the quoted lines of a reply; `/list`
shows every line and where you are writing. `/exit` or `/quit` keeps a draft
for later; `/cancel` discards it. In the fullscreen editor, **Ctrl+G** shows
help and **Ctrl+X** opens the exit choices, including **Keep draft & exit**.
Both editors keep what you have typed if the connection drops.

The fullscreen editor also has these keys:

| Key | What it does |
| --- | --- |
| **Ctrl+K** | Cut the line. Press it again to add the next line. |
| **Ctrl+Y** | Paste the cut lines. |
| **Ctrl+W** | Delete the word before the cursor. In a browser, which closes the tab on Ctrl+W, use **Alt+Backspace**. |
| **Ctrl+R** | Rewrap the quoted (`>`) paragraph under the cursor to fit the screen, keeping its `>`. |

The status line shows how many characters you have used and the limit. The
limit assumes the rest is plain letters: an accented letter or other special
character takes more room and lowers it by one.

Turn the fullscreen editor on or off in **Profile** with **Fullscreen editor
(all writing)**. It is used for mail, posts, your bio and signature, and file
descriptions.

A draft comes back where you started it. A new post's draft is offered when
you return to that board, a reply's when you reply to the same post or
message again. An unfinished letter is shown on the mail screen under
**[D]raft**, with its recipient and subject, and **Compose** offers it before
starting a new one: resume it, delete it, or leave it for later. A draft is
never sent or posted by itself.

Writing a message or a post opens a screen of its own. Mail asks who it is
for first: a user name, or `name@TheirBBS` for someone on a linked BBS. An
empty line or Esc there cancels. The fullscreen editor shows what you are
writing above the text: the recipient or board, and the subject. Nothing is
sent or posted until you choose to on the review screen, which keeps To and
Subject at the top and shows a long message a page at a time. Turn the
pages with PgUp/PgDn or **[N]ext page**/**[P]rev page**. On a board, where
**P** posts, the page keys are **[>]** and **[<]**.

A post or message needs a subject. Leave it empty and mail asks again;
press Esc there to cancel the message. A subject that is too long is caught
as soon as you press Enter, with how many characters to remove, and the
field stays open so you can shorten it. If your signature pushes a message
over its length limit, the review screen says so before you can send it.

## Chat and mail

Choose a chat channel to join the conversation. Type a line and press Enter
to speak. `/help` lists the commands available there.

| Command | Use |
| --- | --- |
| `/who` | See who's online |
| `/join channel` | Switch to another channel |
| `/leave` | Return to the channel picker |
| `/quit` | Leave chat |
| `/msg user text` | Send a live private message |
| `/away text` | Set an away message |
| `/nick name` | Set a chat alias; a bare `/nick` clears it |
| `/dm user` | Invite someone online to a private direct chat |

An alias is always shown with the username beside it, as `Quill|Copperplate`,
so everyone can see who is speaking. An alias that reads like someone else's
username, or like a staff title such as "SysOp", is refused.

A direct-chat invitation opens on your main menu. If you are somewhere else
on the board, a line tells you who invited you; go back to the main menu
within a minute to answer.

Live private messages need the recipient to be online. **E-mail** keeps a
message for later; it is NetBBS mail, not an Internet email account. If live
contact fails, send mail explicitly—chat does not silently turn it into mail.

Signed in as the board's guest, you have no mail: every guest shares that
account, so its mailbox would be everyone's. Register an account of your own
to send and receive mail. Mail sent to the guest account is refused, and so
is mail to an account that is disabled or still waiting for the SysOp's
approval: the To prompt tells you, and mail from another node bounces with
"that account is not taking mail at the moment". A SysOp
can also open mail only from a certain access level; below it, the main menu
offers no **E-mail**.

**E-mail** opens on your Inbox, with how many messages are unread at the top
and how full it is: your mailbox holds 500 messages, and the header counts
them, for example `120 of 500`. From 450 the Inbox warns you. At 500, each
new message removes your oldest message you have already read; unread mail is
never removed, and a mailbox full of unread mail turns new mail away until
you read or delete some. If old mail was removed to make room, the main menu
tells you how many messages went, once.
Each row shows who a message is from, its subject and its date, with `new`
beside mail you have not opened yet. Move with Up/Down and press Enter, or
press a row's number, to read a message. On a long list, **[N]ext page** and
**[P]rev page** (or PgDn and PgUp) turn the page. The other keys:

- **[S]ent** lists the mail you have sent. **[B]ack** there returns to the
  Inbox.
- **[C]ompose** writes a new message.
- **[O]rder** switches the Inbox between newest first and unread first. Your
  choice is remembered.
- **[F]ind** shows only mail with a word in the name or the subject. An empty
  line shows everything again.
- **[U]nread** marks the highlighted message unread, or read without opening
  it. While you read a message, its own **[U]nread** does the same.

You can also write to someone from where you found them, without typing
their address. The compose screen opens with **To** filled in:

- In the **Directory**, open a member's card and press **[M]ail**.
- On **Who's online**, pick a caller and press **[E]-mail** (**[M]** there is
  a live message). It works for a caller on a linked node, and for a caller
  who has turned off live messages.
- On **Previous callers**, press **[M]ail a caller** and type the caller's
  number. A caller whose name is hidden there can't be written to from the
  list.
- While reading a post, **[M]ail author** answers its author privately, with
  the post's `Re:` subject and a quote, as a reply on the board would have.
  You don't need to be allowed to post on that board, and it works for a post
  carried from a linked node too.

After you send the letter, keep it or cancel it, you are back on the screen
you came from, with the result above the prompt. None of these is offered
for yourself, or while mail is closed to you. If the person can't be written
to, for example the guest account or someone on a node that has only just
linked with this one, you are told why when you press the key.

A `!` before a sender's name means their node's identity has changed. The
message explains it when you open it.

Mail from **System** was sent by the BBS itself, not by a person -- for
example, when a moderator turns down a post you wrote, with their reason and
your text. There is no one to reply to, so it has no **Reply**; to question
the decision, write to the moderator or the SysOp.

On a linked node, addresses can use a known, unambiguous name such as
`alice@OtherNode`. Use the address offered by the directory or Who screen,
or the one on the From line of their mail: type it exactly as shown,
capitals included. A node name that itself contains `@` is shown in quotes,
like `bob@"Cats @ Night"`; type the quotes too. Mail checks the address as
you type it and asks again if it can't be used. If more than one linked node
goes by the name you typed, it lists the exact address to type for each.
Check an unexpected node-identity warning with your SysOp. A node that has only just
linked with this one cannot be written to yet: the To prompt says so, and mail
opens once your SysOp establishes that node.

**Reply** works on mail from another node too: the reply goes back to the
address on its From line, with the same `Re:` subject and quote a local reply
gets. If that node can't be written to any more, Reply says why instead of
opening the message. **Sent** shows each message's recipient, with the full
address for mail that went to another node. Your copy stays in Sent even if
the recipient's account is later deleted; it then reads, for example,
`bob (deleted account)`.

Mail to another node also shows where it stands, in Sent's list and on its
Delivery line when you open it:

- **Pending**: on its way; the other node has not confirmed it yet.
- **With relay**: the other node can't be reached directly, so your BBS left
  the letter at a relay for it to collect, and no answer has come back yet.
  If none comes within 14 days, it expires.
- **Delivered**: it is in the recipient's mailbox.
- **Bounced**: the other node sent it back, and the Delivery line says why,
  for example that there is no user by that name there, that their mailbox
  is full of unread mail, or that the node does not trust yours yet.
- **Expired**: no route to that node worked before delivery gave up, so it
  was not delivered. Mail left at a relay expires when no answer came back
  in 14 days; it may still have arrived, and if an answer turns up later,
  Sent shows it as delivered or bounced after all.

A letter shows the way its writer typed it: each line stays a line, and
only a line too wide for your screen wraps. Mail can be in color, written
with pipe codes like `|12` or pasted in, as on a board that allows color.
Profile's **Pos[t] colors** switch covers mail too; with it off, colored mail
reads as plain text.

When mail bounces or expires you are told at the main menu, once, even if it
happened while you were away. Opening the message in Sent counts as being
told. To try again, send it anew.

To stop someone's mail, open a letter from them and press **Bloc[k]
sender**; the same key, now **Unbloc[k] sender**, takes it back. It works
for someone on this board and for someone on a linked node, who is blocked
by their address there, so a node changing its name does not undo it.
**Profile → Blocked mail senders** lists everyone you block, blocks
someone by name (`alice`, or `alice@OtherNode`) before they have written,
and unblocks with **[U]nblock** or by picking a row. Mail already in your
Inbox stays there.

A blocked sender is told: on this board the To prompt and Send say that
you do not accept mail from them, and mail from another node bounces with
that reason. It is never taken and quietly thrown away. The same works the
other way: if you are told a recipient does not accept mail from you, they
have blocked you. Two senders cannot be blocked: **System**, and this
board's SysOp, who has to be able to reach every account on the board.
Blocking covers mail only; live direct messages have their own setting in
Profile.

Channels marked **MRC** connect to a separate public chat network. Your handle
and messages are visible there. MRC private messages are optional in your
profile; they are not confidential from that network. Use `/mrc` for its help
and commands. In **Chat > Multi Relay Chat**, start with `lobby` to discover
network rooms. `/rooms` refreshes the list; return to the picker to choose a
room with its user count and topic, or use `/join room`. Tab completes MRC
subcommands, room names and recipients for `/mrc msg`.
If opening a room is refused, the reason stays visible in the picker while
you choose another room or go back.

The status bar separates people **here** (including their away count) from
people on **MRC**. Remote away counts are unavailable; `?` means the roster
has not arrived, and stale readings are labeled. Profile's MRC color switch
controls incoming body and nickname colors, including scrollback.

Ordinary NetBBS Link mail is protected between nodes but does
not hide its contents from the home-node operators.

### What the SysOp can see

As on most BBSes, the SysOp of the node you are connected to can watch your
session live. They see your screen as you see it, including chat and private
messages, and what you type where it shows on screen. Passwords are not shown
as you type them, so they are never visible this way. You are not told while
it happens, but the node keeps a log of every time a SysOp watches a session.
The last line of chat's `/help` says the same.

The SysOp can also break into your session for a live chat. Your screen
turns into a two-part chat window: the SysOp types in the top half and you
type in the bottom half. When the SysOp ends the chat, your screen comes back
exactly as you left it, with any half-typed line still there. If you were in
a game, the game kept running during the chat. A break-in can't start while
you are typing a password, and anything typed at a password prompt shows as
`*` in the chat.

## Exchange files

Open a file area and select an upload or download action. Use the browser
transfer controls, a browser link offered by the node, or a Zmodem-capable
terminal client. Browser links require the SysOp to configure the web
listener. PuTTY and ordinary SSH clients do not provide Zmodem by themselves.

Treat a transfer link like a password: it grants access to that transfer.
Links expire; request a fresh one if needed. A remote file may first need
to be fetched from its originating node. If that node is unavailable, try
later or tell your SysOp.

## Play a door game

Choose **Games** on the main menu, or a Community's games. Availability
depends on what your SysOp has registered.

- **Retro Trivia:** answer a short round of multiple-choice questions.
- **Voidrunner:** build a persistent space-trading and exploration career.
- **War Dialer:** develop a crew in a shared world of fictional BBS networks.

Each game has its own controls and help. Quit through the game's own menu
to return to NetBBS. A busy game may have a session limit; try again later.
Your SysOp controls the time allowed and can help with a missing or locked save.

## Preferences and help

Use **Profile** for display, editor, and chat preferences, your SSH keys, and
your password. If characters look wrong, try the Unicode-style preference; if
colors are poor, change the color preference. **Ctrl+L** redraws ordinary NetBBS screens after a display problem.
Games may use different controls.

Some resources require an account level, verified age, or verified name.
A name requirement may also disclose the verified name beside contributions
in that resource. Ask the SysOp what is required before sharing identity
information. You cannot grant yourself access by editing your profile.

A SysOp-verified age or name stays on this BBS unless you switch on sharing
over Link for it under **Profile**. Shared, it goes only to the other BBSes
your SysOp has chosen, and the number shown beside "on" is how many that is
now. Switching it off makes this BBS stop giving it out and tells those BBSes
to forget it. One that already copied it cannot be forced to.

To finish, return to the main menu and choose **Log off**. When reporting
a problem, tell the SysOp which screen, connection method, and action caused it.

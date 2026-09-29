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
immediately, without Enter. When typing text, use Enter to submit it.
**Ctrl+U** clears the line you are typing, and at a field that allows it,
Esc leaves it without changing anything.
Follow the action bar on the current screen: letters can mean different
things in different places. **[B]ack** leaves most screens.

| Main-menu choice | What it does |
| --- | --- |
| **Message boards** | Every message board on this node |
| **Chat** | Every chat channel |
| **Files** | Every file area |
| **Games** | Door games, when the SysOp has set some up |
| **Communities** (key **O**) | Topics the SysOp set up, each with its own boards, chat channels, file areas and games |
| **New scan** | See unread activity and resources you have not visited |
| **Find** (key **/**) | Search content available on this node |
| **E-mail** | Read and send persistent NetBBS mail |
| **Who's online** | See callers and available ways to contact them |
| **Profile** | Change your public profile and personal preferences |
| **Moderation** (key **A**) | Posts and uploads waiting for your decision, if the SysOp made you a moderator |
| **Staff** | Your staff console, if the SysOp gave you staff permissions |

Some choices appear only when they have something to show or you have
permission to use them. Search covers content held by this node, including
carried NetBBS Link content; it is not a search of the entire network.

## Read and post

Open a message board, select a post, and use the displayed actions to read,
reply, or start a topic. Follow a resource to make it easier to revisit.
On a moderated board, your post may wait for approval before others see it.

The text editor shows its commands. In the line editor, `/exit` or `/quit`
keeps a post draft for later; `/cancel` discards it. In the fullscreen
editor, **Ctrl+G** shows help and **Ctrl+X** opens the exit choices, including
**Keep draft & exit**. A saved post draft is offered when you return to
that board. A draft is not a published post.

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

Live private messages need the recipient to be online. **E-mail** keeps a
message for later; it is NetBBS mail, not an Internet email account. If live
contact fails, send mail explicitly—chat does not silently turn it into mail.

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

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

Log in with your account. If registration is offered, follow the on-screen
steps; some nodes require approval before you can log in. If registration
is closed, contact the SysOp. Accounts and access rules belong to each node.

## Find your way around

Press a highlighted letter such as **[J]** for **Jump to**. Most menus react
immediately, without Enter. When typing text, use Enter to submit it.
Follow the action bar on the current screen: letters can mean different
things in different places. **[B]ack** leaves most screens.

| Main-menu choice | What it does |
| --- | --- |
| **Communities** | Browse subjects grouping message boards, chat channels, file areas, and doors |
| **Uncategorized** | Browse resources outside a Community |
| **Jump to** | Go straight to a type of resource, including games |
| **New scan** | See unread activity and resources you have not visited |
| **Find** | Search content available on this node |
| **E-mail** | Read and send persistent NetBBS mail |
| **Who's online** | See callers and available ways to contact them |
| **Profile** | Change your public profile and personal preferences |

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
`alice@OtherNode`. Use the address offered by the directory or Who screen.
If a name is ambiguous, NetBBS asks for a more specific identity. Check an
unexpected node-identity warning with your SysOp.

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
has not arrived, and stale readings are labeled. Profile's MRC colour switch
controls incoming body and nickname colours, including scrollback.

Ordinary NetBBS Link mail is protected between nodes but does
not hide its contents from the home-node operators.

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

Choose **Jump to → Games**, or browse a Community's games. Availability
depends on what your SysOp has registered.

- **Retro Trivia:** answer a short round of multiple-choice questions.
- **Voidrunner:** build a persistent space-trading and exploration career.
- **War Dialer:** develop a crew in a shared world of fictional BBS networks.

Each game has its own controls and help. Quit through the game's own menu
to return to NetBBS. A busy game may have a session limit; try again later.
Your SysOp controls the time allowed and can help with a missing or locked save.

## Preferences and help

Use **Profile** for display, editor, and chat preferences. If characters look
wrong, try the Unicode-style preference; if colors are poor, change the color
preference. **Ctrl+L** redraws ordinary NetBBS screens after a display problem.
Games may use different controls.

Some resources require an account level, verified age, or verified name.
A name requirement may also disclose the verified name beside contributions
in that resource. Ask the SysOp what is required before sharing identity
information. You cannot grant yourself access by editing your profile.

To finish, return to the main menu and choose **Log off**. When reporting
a problem, tell the SysOp which screen, connection method, and action caused it.

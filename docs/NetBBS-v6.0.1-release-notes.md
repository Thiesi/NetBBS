# NetBBS v6.0.1

**Superseded: 6.0.1 was never released.** It sat unreleased on `main` while
the Voidrunner and War Dialer overhauls landed, and everything described here
shipped in [v7.0.0](NetBBS-v7.0.0-release-notes.md) instead. This file is kept
for the MRC detail it records; there is no 6.0.1 wheel or tag to install.

A fix release for the MRC (Multi Relay Chat) bridge, the first from
reading the hub operator's protocol specification (MRCDoc, revision 1.26),
to which access was granted on 2026-09-09. One database migration, described
under the field limits below; it matters only to a node that mapped or opened
an MRC room with a name longer than 20 characters.

## Away state reaches the hub in the documented form

The bridge mirrored a caller's `/away` with an `AFK` command that the
protocol does not define, so on the real hub every `/away`, return and
reconnect earned the caller an error reply and their away state never
showed in the network's user lists. It now sends the documented
`STATUS AFK <message>` together with the `IAMHERE:AWAY` presence report,
repeats both on every announcement and reconnect, and reports
`IAMHERE:ACTIVE` on return. The per-minute presence report every announced
caller sends now carries their away or active state as well. Away messages
are cut to the network's 55 characters, and the caller is told when that
happens.

The specification names no command that clears the away flag, so the hub's
own activity tracking decides when a returned caller stops showing as away.

## Private messages carry no site on the wire

The specification reserves packet field 5 for future extensions such as
encryption and says to drop every use of it as a destination board. Private
messages from NetBBS put the recipient's last-seen board there as a routing
hint; they now leave the field empty, as every other client does. Nothing is
lost: the hub keeps nicks unique across boards, so the nick alone is the
address. The `nick@board` a sender sees in their own echo is unchanged.

## Outbound lines keep to the hub's rate

The hub accepts one message per half second from each user. NetBBS paced
its sending per node and admitted a long line's two or three chunks at
once, so the tail of such a line could be dropped by the hub without anyone
here noticing. Packets from one caller now leave at least half a second
apart, while other callers' packets and the node's own housekeeping go out
in between.

## Field limits and the handshake follow the specification

Room names are at most 20 characters on the network, topics 55, passwords
20 and room passwords 32. A room name a caller opens or a SysOp maps that
is longer than 20 characters is now refused with a message rather than
quietly shortened into a room the hub would know by another name. Topics
and passwords are refused at their limits instead of at the packet's.

The migration in this release applies the same limit to rooms recorded
before it existed. A channel you mapped to a room name longer than 20
characters is unmapped, because the hub never knew the room under that
name: after upgrading, open `[C]ontent` → Cha`[N]`nels → the channel →
`[M]RC room` and map it again with a name of at most 20 characters. A room
a caller opened under such a name is shortened to its first 20 characters
when no other room holds them, otherwise it becomes an ordinary channel.
The migration's description in the node's migration table names what it
did. The
connect handshake now names the client the way the specification asks,
`NETBBS/<Os.arch>/<NetBBS version>`, so `/mrc bbses` on other boards shows
this software and its version correctly.

## What the hub is told about callers, and about this node

Two new switches under Inter-BBS chat (MRC), both off, let a SysOp send the
hub each announced caller's connecting IP address (`USERIP`, which the hub
uses to ban one caller rather than a whole board, and without which it may
drop a caller from room routing) and their security level with the SysOp's
name (`BBSMETA`). Terminal sizes are always sent so the hub can format wide
replies. The node now advertises the capabilities it really has (colour,
CTCP, hub-directed room moves, graceful goodbye), and the bridge status
screen shows the round trip to the hub, measured from each keepalive.

## Smaller things the specification settled

Hub notices (`NOTIFY`) now reach every bridged channel instead of being
dropped. The network-size line gains the hub's own activity level. MRC
handles are shown with underscores as spaces, as the specification asks.
Typing `!identify`, `!register`, `!update` or `!roompass` as chat is
refused in every channel while the node has an MRC bridge, because the hub
is moving those to chat-text helpers and the password would otherwise be
recorded as chat and relayed as soon as the channel is bridged; the `/mrc`
forms ask for it without echo. A new Profile switch, on by default, lets a
caller stop the hub from answering `LASTSEEN` questions about their handle.

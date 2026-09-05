# NetBBS v5.8.0

This release lets callers reach any room on the MRC (Multi Relay Chat)
network from NetBBS's own chat, without the SysOp mapping each room first
and without leaving for an external client. It includes one database
migration (schema version 61 to 62) and no protocol version changes.

## Open rooms

With MRC enabled, a SysOp can now switch on open rooms under
`[S]ettings` → `[I]nter-BBS chat (MRC)`. The chat channel picker then gains
a `[Multi Relay Chat]` entry that lists the rooms open on this node with
their local and network occupancy, rooms the node has heard of, and
`[Open a room by name]`. A room a caller opens becomes a channel of its own
named `mrc:<room>`, with the level, age and name-requirement gates the SysOp
sets for open rooms, and everything a channel already has: scrollback, the
moderation set, follows and search. Inside one, `/join <room>` opens another
room, `/join mrc:<room>` works from anywhere, and `/rooms` asks the hub.

Open rooms are bounded. A cap (default 32) refuses further openings without
evicting anyone. A retention period (default 7 days) retires a room nobody
is in, nobody follows and nobody has used, together with its scrollback.
A blocklist names rooms callers may not open. On an open room's channel
screen the SysOp can `[A]dopt` it as an ordinary bridged channel or
`Re[t]ire` it at once. Open rooms are never shared over NetBBS Link, and
the `mrc:` name prefix is reserved for them. The node status screen shows
open rooms against the cap and what the sweeper has retired.

MRC allows one identity per caller in one room, so a second session of the
same account entering a different MRC room is refused naming the room it
already holds. Mapped channels, the bridge's colours, hub commands and CTCP
from v5.7.1 are unchanged.

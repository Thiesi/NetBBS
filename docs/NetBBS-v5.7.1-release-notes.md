# NetBBS v5.7.1

A patch release for the MRC (Multi Relay Chat) bridge introduced in v5.7.0.
It corrects how chat lines cross the wire in both directions, shows the
network's colours, answers CTCP, and lets callers ask the hub things from
inside a bridged channel. There are no database migrations and no protocol
version changes.

## The wire convention, fixed

Every MRC client embeds the sender's own coloured handle inside the message
body and displays inbound bodies as they arrive; the hub adds nothing. The
v5.7.0 bridge sent bare text and recorded inbound bodies verbatim, so a NetBBS
caller's lines reached other boards with no name attached, and lines from
other boards showed the name twice on NetBBS. Both are corrected: a caller's
line now goes out as `<handle> text` in the network's colour codes, and an
inbound line is recorded under one name, as a message or as an action.

## Colours

MRC users colour their lines with Mystic-style `|NN` codes. NetBBS now shows
those colours in bridged channels, live and in scrollback, after the usual
sanitization. Callers who prefer plain text switch them off under
`[P]rofile` → `[I]nter-BBS chat colours`. Typed codes in a caller's own lines
are still not relayed.

## Asking the hub

`/mrc rooms`, `/mrc who`, `/mrc bbses [search]`, `/mrc info <bbs>`,
`/mrc motd`, `/mrc stats`, `/mrc help`, `/mrc lastseen <nick>`, `/mrc topics`
and `/mrc send <command>` send the corresponding hub command as the caller's
own nick; the hub's reply is shown to that caller alone, bounded per caller.
Network-wide broadcasts appear in every bridged channel as `[MRC broadcast]`.
A caller the hub moves out of the mapped room is told and announced there
again; a caller the hub renames is told the new name; a hub `TERMINATE` stops
the link until the MRC settings are saved again.

## CTCP

The bridge answers VERSION, TIME (in UTC), PING and CLIENTINFO requests for
any announced caller, bounded per remote sender, and `/mrc ctcp <nick>
<command>` asks another user's client the same. Nothing needs configuring.

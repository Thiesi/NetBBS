# NetBBS v5.10.0

This release lets callers who want them exchange private messages with
users on the MRC (Multi Relay Chat) network. There is no database migration
and no protocol version change.

## Private MRC messages, opt-in

Private messages from MRC users are off by default, as before: a caller
who has not switched them on is told once per sender that somebody tried,
and nothing else is shown. A caller who wants them switches them on under
`[P]rofile` → `[P]rivate MRC messages`; the switch applies the next time
they enter an MRC room and covers both directions.

With the switch on, a private line from an MRC user rings the bell and
appears as `[MRC private] bob@Other: text`, to that caller alone, with the
network's colours. `/mrc msg <nick> <text>` answers anyone on the network
and `/mrc r <text>` answers whoever wrote last. Outbound lines carry the
caller's handle in the house style so the other board shows who wrote
them, and are routed to the board the recipient was last seen on.

The first private line in a session, sent or received, comes with one
note: private MRC messages are not private on that network, because the
hub and any client can read or spoof them. Private lines are never stored
anywhere on the node: not in scrollback, not in search, not in any log.

There is nothing for the SysOp to configure. The Handbook's MRC section
describes what callers see.

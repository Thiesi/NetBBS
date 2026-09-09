# NetBBS v6.0.1

A fix release for the MRC (Multi Relay Chat) bridge, the first from
reading the hub operator's protocol specification (MRCDoc, revision 1.26),
to which access was granted on 2026-09-09. No database migration.

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

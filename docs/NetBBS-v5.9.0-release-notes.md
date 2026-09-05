# NetBBS v5.9.0

This release makes the MRC (Multi Relay Chat) rooms feel inhabited and lets
callers carry the presence they already have here onto the network. There
is no database migration and no protocol version change.

## Presence and welcome on MRC

A caller's `/away` is mirrored to the network as their MRC away state and
repeated on every reconnect; returning clears it. The first MRC room a
caller enters in a session shows the hub's banner and message of the day,
once per session; `/mrc motd` asks again.

The network's size now appears where callers look for company: above
Who's online, in the chat picker's Multi Relay Chat section, and on the
bridge status screen, as "MRC: 41 users on 12 boards" with the reading's
age, refreshed every few minutes while anyone here is on the network.

In a room a caller opened, the topic is the hub's: it shows on the status
line as the hub sets it, and `/topic <text>` there asks the hub rather than
changing anything locally. A channel the SysOp mapped keeps the local
`/topic`.

`/mrc register`, `/mrc identify`, `/mrc update password` and `/mrc roompass`
ask for the password separately, with echo off, and send it once. NetBBS
stores no MRC credentials, and the password reaches no scrollback, log or
input history.

Each caller can pick the colour their handle wears on MRC under
`[P]rofile` → `[Y]our MRC nick colour`, one of the sixteen CGA colours MRC
clients understand; the default stays the house yellow.

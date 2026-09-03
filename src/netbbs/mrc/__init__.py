"""
MRC (Multi Relay Chat) bridge (design doc §16, issue #165 / #275).

A protocol-translation gateway at the boundary of NetBBS's own chat:
`netbbs.mrc.protocol` speaks MRC's tilde-delimited wire format,
`netbbs.mrc.settings` holds the DB-backed node/per-channel
configuration, and `netbbs.mrc.bridge.MrcBridge` owns the one outbound
hub socket per node and fans local channel traffic through it. Nothing
in `netbbs.chat` or `netbbs.link` imports this package -- the bridge is
attached from `netbbs.net.chat_flow` at the same call sites
`netbbs.link.realtime_channels.LiveChannelBridge` already uses, and is
independent of whether Link is configured at all.
"""

# Bot API v1

Configure instances under **SysOp > Content > Open chat bots**. Nothing runs
until an instance has allowed channels, is enabled, and is saved. The empty
program array `[]` selects bundled Baud. For an installed external bot, supply
a JSON argument array such as `["/usr/pkg/bin/python3.11", "-u", "/opt/bot.py"]`.
Arguments are executed directly, without a shell; use absolute program and
script paths. The working directory is the instance's state directory.

Baud and external bots have identical host permissions. This slice grants
`addressed` only, for explicitly allowed channels. It does not provide
channel-wide reads, source events, statistics/seen queries or model requests.
Linked channels also need an explicit per-channel **Linked approvals** entry,
confirmed on Save. MRC channels are refused, even with a paused bridge.
A grant is checked again on dispatch and
publication, including changes made while the process is running.

Programs run as the NetBBS OS user. Queue, protocol and output bounds protect
the host API; they do not sandbox arbitrary installed code or limit its OS
permissions. Install trusted programs. On Unix, supervision ends the process
group; on Windows, it ends the direct child. Programs must not daemonize or
leave helper processes behind. Only a small environment (paths, home, temporary
directory and locale variables) is inherited; stdout is protocol-only. Stderr
is drained without retaining it, to avoid unbounded logs or prompt disclosure.

## Handshake and events

Each line is one UTF-8 JSON object, at most 32 KiB including the newline.
`BOT_API_VERSION = 1`. The host sends:

```json
{"type":"hello","version":1,"name":"Baud.bot","scope":"addressed","channels":["lobby"],"temperament":"friendly","state":{}}
```

The bot must answer within five seconds with exactly:

```json
{"type":"ready","version":1}
```

The host then sends addressed messages and one timer tick per second:

```json
{"type":"message","request":"opaque-token","channel":"lobby","caller":{"id":7,"name":"alice"},"text":"help"}
{"type":"tick","time":1791716400.0}
```

Treat caller ids as opaque JSON values: local callers retain their integer
account id; remote callers use `link:<home-fingerprint>:<canonical-username>`.
The latter cannot collide with a local id or the same username on another
node. Remote caller names are node-qualified for display. A message comes only
from an accepted live human send, locally or through an authenticated bot
request from the caller's home node; bot output, announcements and scrollback cannot create
requests. `Baud.bot: help` names an instance. `!help` selects at most one local
default for the channel, even in Linked chat. `Baud.bot@Node: help` names one
remote instance, using Link's unique friendly name, DNS name or fingerprint
resolution. Unknown or ambiguous names are refused. Both nodes must carry
the Linked channel and advertise live bot support. Ordinary chat creates no event. A request token is
owned by that instance and expires after 60 seconds. There are at most 32
outstanding requests per instance. Busy, unavailable, expired and refused
requests give the caller an explanation.

## Actions and results

Every action needs a string `id` of at most 64 characters. Match results by id;
events can arrive between a request and its result. Flush each output line.

```json
{"id":"1","action":"reply","request":"opaque-token","text":"alice: Happy to help!"}
{"id":"2","action":"say","channel":"lobby","text":"alice: reminder #1: check the oven"}
{"id":"3","action":"state.get"}
{"id":"4","action":"state.set","value":{"reminders":[]}}
```

`reply` publishes one line in the original request's channel; it is **public**,
not a private message. `say` publishes in an allowed channel without requiring
a current request, for reminders. The host chooses the author identity. Both
actions store the line in ordinary chat history and apply the same policy,
mute and rate checks. Text must contain 1-500 characters, no line breaks, tabs
or terminal controls. Approved Linked output uses ordinary signed channel
events and live delivery; MRC never receives it. A remote request's public
reply is also delivered directly to its requesting node, so it can arrive
while the channel origin is offline. Normal live-message deduplication hides
any second copy through the origin. Reminders use the ordinary channel route.

Running bots have separate channel presence, including bots on subscriber
nodes. They never enter human or node-wide online counts. `/mute Baud.bot`
controls a local instance; `/mute Baud.bot@node-fingerprint` filters that
remote bot on this node, including history, search and its roster entry.
`/unmute` reverses either control. Remote mutes do not change signed events
or another node's policy. `/bots local` and `/bots none` remain channel-wide
receiving controls.

The optional signed endpoint capability `chat_bots_v1` enables three live
frames (realtime protocol version remains 4):

| Frame | Payload | Authority |
| --- | --- | --- |
| `bot_request` | `channel_id`, `bot`, `caller`, `text` | Direct from the caller's authenticated home; the frame message id identifies the request. |
| `bot_result` | `request_id`, `error` | Direct from the expected bot host; empty error means its public reply was published. |
| `bot_presence` | `channel_id`, `author_node_fingerprint`, `labels` | Own presence, or another node's presence forwarded by the channel origin. Empty labels withdraw it. |

These frames are sent only to advertising peers. Older nodes can still carry
ordinary bot output. Presence contains at most 16 labels per node/channel;
receivers retain at most 512 node/channel rosters and discard disconnected
sources. Bot subscriptions remain live without a human in the channel and
reconnect while granted. There are at most 256 outgoing requests, each with
a 75-second end-to-end deadline including dialing; the host still allows at
most 32 pending requests per instance. The last 1,024 incoming request ids
are remembered to reject duplicates. Requests are never persisted, replayed
or automatically resent after disconnect; a timeout advises checking the
channel before retrying.

```json
{"type":"result","id":"1","ok":true,"value":{"message_id":42}}
{"type":"result","id":"2","ok":false,"error":"Baud.bot is muted here."}
```

`state.get` returns the instance's JSON value. `state.set` atomically replaces
it, up to 16 KiB encoded JSON; success returns `null`. A bot cannot select a
different instance's state through the API. Wait for success before promising
the caller that a reminder was saved. State lives at
`<database-filename>.bots/<original-instance-name>/state.json`; renaming keeps
that directory. **Node backups do not include it yet**; with the node stopped,
copy the `.bots` directory alongside the database. No model context is stored.

Unknown actions, including ungranted queries, return a refusal. A malformed
line, failed handshake or more than 240 actions/minute ends the process. The
host queue holds at most 64 events/results; stalled pipe writes time out after
five seconds. Output is limited to the configured 1-120 lines/hour per bot per
channel (default 60), retained across restarts. Node limits are 16 instances
and 32 allowed channels each. Exit/failure restarts with exponential backoff
from one to 60 seconds. Kill controls disable the instance persistently.

## Bundled Baud

Commands: `help`, `remind <1s-30d> <text>`, `reminders`, `cancel <id>`.
Reminder text is capped at 160 characters. Baud holds up to eight reminders
per caller and 48 per instance, also subject to the state byte limit. Listing
and cancelling work only for the caller's reminders in the current channel,
so a public query cannot expose a reminder from a private channel.

Due reminders wait through shutdowns and retry after refused output. They are
removed after successful publication; a crash between publication and saving
that removal can repeat one. This is not an exactly-once delivery guarantee.
If a channel is renamed or a grant removed, old reminders retain their original
destination and cannot migrate to another channel automatically.

Baud's standalone implementation in `src/netbbs/bots/baud.py` is an API example:
it uses standard input/output only and has no database or filesystem access.

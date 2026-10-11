# Bot API v1

Configure instances under **SysOp > Content > Open chat bots**. Nothing runs
until an instance has allowed channels, is enabled, and is saved. The empty
program array `[]` selects bundled Baud. For an installed external bot, supply
a JSON argument array such as `["/usr/pkg/bin/python3.11", "-u", "/opt/bot.py"]`.
Arguments are executed directly, without a shell; use absolute program and
script paths. The working directory is the instance's state directory.

Baud and external bots have identical host permissions. Each instance starts
with `addressed` only, for explicitly allowed channels. **Read whole channels**
grants live human messages and actions in a named subset of those channels;
**Presence-event channels** separately grants their live joins and leaves.
Both lists start empty. Greetings require the presence grant, not channel
reading. Statistics and last-seen queries have separate, default-off grants.
Board and file-area event sources are separately granted, with one allowed
**Announcement channel** per instance. Sources must be readable by everyone,
including inherited Community gates; restricted mappings are refused. This
conservative audience rule also holds for Linked destinations. **Baud post
announcements** and **Baud file announcements** are separate, default-off
abilities requiring their corresponding source grants. Model conversation has
its own default-off grant and a separate node-wide provider switch.
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
{"type":"hello","version":1,"name":"Baud.bot","scope":"addressed","channels":["lobby"],"temperament":"friendly","state":{},"queries":["stats","seen"],"reading_channels":[],"event_channels":[],"greetings":false,"board_sources":[],"file_sources":[],"announcement_channel":null,"announce_posts":false,"announce_files":false,"model_conversation":false}
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
the Linked channel and advertise live bot support. Ordinary chat creates no
addressed request. A request token is
owned by that instance and expires after 60 seconds. There are at most 32
outstanding requests per instance. Busy, unavailable, expired and refused
requests give the caller an explanation.

With the corresponding explicit source grants, additional live events are:

```json
{"type":"channel","channel":"lobby","kind":"message","caller":{"id":7,"name":"alice"},"text":"Hello everyone"}
{"type":"presence","channel":"lobby","kind":"join","caller":{"id":7,"name":"alice"},"text":""}
```

Channel kinds are `message` and `action`; presence kinds are `join` and
`leave`. These observations have no request token and cannot authorize a query
or reply. An addressed line may also be observed by a channel reader, but
creates at most one addressed request. Ordinary chat never invokes Baud's
commands or model conversation. Bot/door output, MRC, history, presence
snapshots and synchronization catch-up do not produce observations. Linked
observations use the host's accepted live stream and existing presence
topology, not a synthetic join for every person in a reconnect snapshot.
Source grants, policy and mutes are checked when dispatching each event. A
full event queue drops the observation and records the reason in SysOp status;
events are not replayed later.

Board/file source grants supply bounded metadata events, never post bodies or
file contents:

```json
{"type":"post","event":"opaque-event-token","channel":"lobby","source":"News","item_id":"content-id","title":"Node update","text":"New post in News: Node update"}
```

File events use `type: "file"` and a filename as `title`. Sources are named
explicitly in the editor; at most 32 boards/file areas combined are allowed.
Events cover newly approved posts/uploads and newly catalogued carried files.
Pending content is announced only after approval, edits do not reannounce a
post, and downloading an already-catalogued file creates no second event.
Existing history is skipped when a bot process starts or restarts. New arrivals
through Link may produce source events while it runs, subject to current trust
and visibility. Channel catch-up still never produces channel observations or
addressed requests.

The host retains at most 1,024 content-event references, without titles/bodies,
and dispatches at most 32 per poll per instance. There are at most 32 pending
source-event tokens per instance, expiring after 60 seconds. Queue/window
overruns record an explanation in SysOp status. These are best-effort notices,
not a durable delivery queue: stopped programs, expired tokens and refused
announcements are not replayed. Ordinary content remains in its board/area.

Granting channel reading warns the SysOp that the program receives other
callers' conversation and requires a final default-No confirmation on Save.
New reading and Linked grants share that one confirmation; declining preserves
the draft and persists nothing. The heading and `/who` disclose each reader; changes
also notify callers already in the channel. Disclosure describes the enabled
grant even while a local process is restarting. Receiving mutes or bot-hiding
policy or a bot's user-level trust block do not hide a remote reader's disclosure: they cannot revoke access
at its host.

## Actions and results

Every action needs a string `id` of at most 64 characters. Match results by id;
events can arrive between a request and its result. Flush each output line.

```json
{"id":"1","action":"reply","request":"opaque-token","text":"alice: Happy to help!"}
{"id":"2","action":"say","channel":"lobby","text":"alice: reminder #1: check the oven"}
{"id":"3","action":"state.get"}
{"id":"4","action":"state.set","value":{"reminders":[]}}
{"id":"5","action":"query","request":"opaque-token","query":"stats"}
{"id":"6","action":"query","request":"opaque-token","query":"seen","target":"alice"}
{"id":"7","action":"announce","event":"opaque-event-token"}
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

`announce` consumes this instance's live source-event token. The host chooses
the destination and public summary; action-supplied text or channel cannot
replace them. The source grant, destination, public audience, current content
and trust visibility are checked again inside the publication transaction.
A source made restricted/hidden or content removed/expired while the program
was processing it is refused before publishing or debiting output rate.
Changing the destination discards old events rather than moving them. It uses
the same ordinary chat/Link output, mutes and hourly rate as `say`/`reply`.
As elsewhere in this API, installed programs remain trusted OS processes;
the host's grant checks do not sandbox arbitrary code.

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

The additional optional capability `chat_bot_readers_v1` enables `bot_readers`,
with the same bounded payload and origin-only relay rules as `bot_presence`.
Its labels are bots with whole-channel reading grants; an empty list withdraws
them. Each receiver retains at most 512 reader rosters and discards disconnected
sources. It is sent only to peers advertising both bot capabilities; older
nodes continue carrying ordinary bot traffic but cannot display this extended
reader disclosure. Realtime protocol version remains 4.

```json
{"type":"result","id":"1","ok":true,"value":{"message_id":42}}
{"type":"result","id":"2","ok":false,"error":"Baud.bot is muted here."}
```

`query` requires a live request token belonging to that instance. The host
uses the caller and channel attached to the token, checks the current query
grant and channel policy, and rechecks the local account or remote Link trust.
A successful query returns an object in `value`, including a public-safe
`text` suitable for a reply. Query success or refusal does not consume the
token; the bot can still send its one public `reply`. No arbitrary database
access is exposed.

- `stats`: node display name (`node`), count of online human accounts
  (`online_humans`) and uptime in seconds (`uptime_seconds`). Bots and multiple
  sessions of one account do not inflate the count. Uptime starts with the
  node's bot runtime. No private resource counts are included.
- `seen`: one local account name on the bot's host, including when requested
  over Link. An available result has `available: true`, `username` and
  `online`. Offline results add `at` (UTC timestamp) and `basis` (`login` or
  `logout`) for the latest recorded visit boundary. An interrupted visit
  reports its actual login, never the later restart's detection time.
  Hidden session history, disabled/pending accounts and unknown names return
  the same `available: false` response. Online status is already public node
  presence. Even a SysOp request cannot publish hidden history; private-channel
  names and activity are never returned.

`state.get` returns the instance's JSON value. `state.set` atomically replaces
it, up to 16 KiB encoded JSON; success returns `null`. A bot cannot select a
different instance's state through the API. Wait for success before promising
the caller that a reminder was saved. State lives at
`<database-filename>.bots/<original-instance-name>/state.json`; renaming keeps
that directory. Node backups include API-managed `state.json` files as the
checksummed `bot-state` component. Capture reads complete atomic files before
the database snapshot so caller ids are not newer than that snapshot. Restore
replaces the entire live `.bots` directory with the archived generation;
restoring an older archive with no bot state removes newer live state, keeping
it in the restore rollback directory. Use a build supporting this component
to restore it. No model context is stored.

Backups allow at most 64 instance directories with one state value of up to
16 KiB each. Invalid JSON, symlinks, oversized values and case-colliding names
are refused. Executables, temporary files and extra files written directly by
an external program are outside the state API and are not captured. Install
programs separately and use the state API for persistent bot data.

**Delete selected** removes a stopped instance and its entire state directory,
including reminders and any extra program-owned files there. Kill it first
and wait for the process to stop. The final confirmation defaults to No;
backing out changes nothing. Channel history and existing backups remain.
Recreating the name starts with empty state. Restoring an earlier node backup
can bring back the deleted instance and its captured state.

Unknown actions, including ungranted queries, return a refusal. A malformed
line, failed handshake or more than 240 actions/minute ends the process. The
host queue holds at most 64 events/results; stalled pipe writes time out after
five seconds. Output is limited to the configured 1-120 lines/hour per bot per
channel (default 60), retained across restarts. Node limits are 16 instances
and 32 allowed channels each. Exit/failure restarts with exponential backoff
from one to 60 seconds. Kill controls disable the instance persistently.

## Bundled Baud

Commands: `help`, `remind <1s-30d> <text>`, `reminders`, `cancel <id>`.
With the corresponding grants, Baud also offers `stats` and
`seen <local-account>` and lists them in `help`. Both replies are public.
With **Grant model conversation**, other addressed text requests the host's
model service; `reset` clears this caller's temporary context here. Ordinary
commands stay local and remain responsive during a provider request.
The separate **Baud greetings** toggle greets live joins in presence-event
channels. Greetings obey the same mutes, channel policy and hourly output limit
as replies and reminders, and require no whole-channel reading grant.
Post/file announcements likewise need only their explicit source grants and
destination, with the corresponding Baud ability enabled. They require no
channel reading or presence access.
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

## Optional model conversation

Under **Chat bots > Model settings**, the initial API base URL is
`http://127.0.0.1:11434/v1` for an operator-managed Ollama server. A local
llama.cpp server or another OpenAI-compatible endpoint can be configured
instead. Set an installed/available model name; NetBBS does not install or
download models. The optional `netbbs[bots]` extra supplies aiohttp (also used
by `netbbs[web]`); ordinary bots need neither it nor a model server. No HTTP
request occurs while editing settings. Enable the model service and each
instance's **Grant model conversation** separately. Save confirms where
addressed conversation goes. Use HTTPS for a remote provider.
The handshake's `model_conversation` reflects both switches. Changing the
node-wide switch restarts granted instances so their help and command handling
track availability; their persisted grants and reminder state remain intact.

The API base URL gets `/chat/completions` appended. Calls are non-streaming
with `messages`, `model`, `store: false` and a bounded output limit. Select
`max_completion_tokens` for current OpenAI models; the default `max_tokens`
matches [Ollama's documented fields](https://docs.ollama.com/api/openai-compatibility).
NetBBS accepts text from
`choices[0].message.content` and usage from `prompt_tokens`,
`completion_tokens`, `total_tokens`. See the
[official Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
Provider retention policies still apply; `store: false` is not a promise that
the provider retains nothing. Tools, images, attachments and automatic retries
are not used. Redirects are refused, responses are capped at 64 KiB and requests
time out after 40 seconds. At most two model requests run on a node at once.

External programs use the same actions as Baud:

```json
{"id":"7","action":"model","request":"opaque-token"}
{"id":"8","action":"model.reset","request":"opaque-token"}
```

These return an immediate `result` with `value: {"pending": true}`. The host
then publishes the reply or sends a refusal to the requesting caller. The
request token fixes the original input, caller and channel: program-supplied
prompts, identities or destinations cannot substitute them. Observations and
source-event tokens cannot request a model. API keys stay in the host; they
never enter the program's handshake, environment or result. Installed programs
remain trusted OS-user code, as described above.

The first conversational request only publishes a notice naming the provider,
context allowance and prompt-logging policy. It is not forwarded or added to
context. Submit again to proceed. Disclosure repeats after settings changes,
context expiry, reset or restart. Model input consists of the host's configured
persona with the active bot's chat identity, this addressed message and at most
0-20 prior lines of this caller's exchange with this bot in this channel. It
never includes ordinary conversation
or another caller's exchange, even if the bot has a whole-channel reading grant.
Only successfully published replies enter context. Context is memory-only,
limited to 256 caller/bot/channel entries, expires after 30 idle minutes, and
clears on bot restart. Eviction requires a new disclosure. Reset does not erase
channel history, existing backups, optional prompt logs or provider records.
Replies remain public, sanitized and capped at the configured 80-500 characters.
Disclosure and reset notices have their own fixed wording and the ordinary
500-character chat limit.

Guests are excluded by default using the designated guest account, regardless
of its name. If explicitly admitted they share that account's quota and context.
Link's optional `chat_bot_callers_v1` capability adds bounded `access` fields
to `bot_request`: `level` (integer 0-255) and `authenticated` (boolean). Only
the caller's authenticated home node supplies them, directly to the bot host;
they authorize model admission only and grant no local account privileges.
Peers lacking this extension retain ordinary bot commands, but their model
requests receive an explicit refusal rather than assuming authentication.
Current local account state and remote node/user trust are rechecked.

Limits apply across bot instances: UTC daily/monthly node token budgets,
per-caller daily requests and cooldown, and per-channel requests per minute.
Normal bot output rates, mutes and channel policy also apply. Before HTTP, a
SQLite transaction reserves UTF-8 input bytes plus framing allowance and the
configured maximum output tokens. Reported usage replaces this conservative
estimate. Unmetered, failed or interrupted calls retain their reservation;
restarting cannot refund potentially charged work. Actual provider usage can
exceed an estimate; it is still counted and blocks further calls as appropriate.

Optional money accounting uses operator-entered prices per million input and
output tokens, in one operator-chosen currency, with optional day/month caps.
Prices are never fetched and this is not a provider invoice. Zero monetary
budgets disable the extra money limit; token budgets always apply. **Usage**
shows counts, reported/reserved tokens and cost, distinct callers, the top 20
callers and limits hit. The ledger admits at most 20,000 calls per UTC month.
Hourly cleanup keeps the current month and one prior day for rollover cooldowns.
API keys use the same protected SQLite/backup boundary as FTN passwords and are
masked in the editor. Prompt logging defaults off. If enabled, only submitted
inputs are stored with the bounded ledger; disclosure inputs and unrelated chat
are never logged by the model service. Turning logging off stops new logging;
existing entries age out normally and existing backups may retain them.

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
is drained without retaining it, to avoid unbounded logs or prompt disclosure;
only a console trial ([Try](#try-a-bot-in-the-console)) shows an excerpt.

## Handshake and events

Each line is one UTF-8 JSON object, at most 32 KiB including the newline.
`BOT_API_VERSION = 1`. The host sends:

```json
{"type":"hello","version":1,"name":"Baud.bot","scope":"addressed","channels":["lobby"],"temperament":"friendly","phrases":{},"state":{},"queries":["stats","seen"],"reading_channels":[],"event_channels":[],"greetings":false,"board_sources":[],"file_sources":[],"announcement_channel":null,"announce_posts":false,"announce_files":false,"door_sources":[],"announce_doors":false,"model_conversation":false,"timezone":"Europe/Berlin","blocks":true,"quotes":false,"polls":false,"linked_channels":[],"node":"Roanoke"}
```

`timezone` is the IANA name of the node's display clock (§6.3: one clock for
the node, no per-caller zone), for a bot that reads or shows wall-clock times.
When the SysOp changes the display zone, running instances restart, as they do
when the model-service switch changes.

`blocks` is true when the host takes a reply's titled block (see
[Titled blocks](#titled-blocks)); an older host sends no `blocks` and refuses
a reply that carries one.

`linked_channels` lists the channels among `channels` that are shared over
Link and approved for this instance: callers on other BBSes see its lines
there and reach it as `Name.bot@<node>: <text>`, where `node` is this node's
display name. `quotes` and `polls` are Baud's two social switches (below);
another bot may ignore them.

`temperament` is `friendly`, `terse` or `retro`. `phrases` holds the SysOp's
overrides of the temperament's fixed phrases, keyed `greeting`, `help`, `busy`,
`limit` and `full`; a key is present only when the SysOp wrote that phrase, so an
absent key means "use your preset". The host checks each phrase on Save: plain
text on one line, no placeholder but `{name}` (the caller's name; `{{` and `}}`
are literal braces), and at most 400 characters with each `{name}` counted as
32, so `<caller>: <phrase>` fits one 500-character line; `help` is held to 200,
a block row's length, since it opens Baud's command list. Any bot may honour
them; Baud does (see below).

The bot must answer within 30 seconds, counted from the hello, with exactly:

```json
{"type":"ready","version":1}
```

The host then sends addressed messages and one timer tick per second:

```json
{"type":"message","request":"opaque-token","channel":"lobby","caller":{"id":7,"name":"alice","moderator":false,"guest":false},"text":"help","private":true,"asked_privately":false}
{"type":"tick","time":1791716400.0}
```

`private` says whether a private answer can reach this caller (see
[Private answers](#private-answers)); it is false only for a caller on a
linked node that cannot take one. `asked_privately` is true when the caller
asked with `/msg Baud.bot <text>` or in a `/private Baud.bot` conversation;
such a request takes only a private answer.

`caller.moderator` is true when a local caller holds moderate permission on
this channel, which the SysOp always does; a caller from another BBS is never
a moderator here, whatever they hold at home. `caller.guest` is true for the
node's guest account, and for a caller from another BBS whose home node says
the account is not authenticated (`access.authenticated` false). Both are
facts for a bot's own rules, such as who may delete a stored quote; they grant
nothing at the host.

Treat caller ids as opaque JSON values: local callers retain their integer
account id; remote callers use `link:<home-fingerprint>:<canonical-username>`.
The latter cannot collide with a local id or the same username on another
node. Remote caller names are node-qualified for display. A message comes only
from an accepted live human send, locally or through an authenticated bot
request from the caller's home node; bot output, announcements and scrollback cannot create
requests. `Baud.bot: help` names an instance. `!help` selects at most one local
default for the channel, even in Linked chat. Shorthand is `!` followed by a
command word: `!!!` or `! help` is ordinary chat. So is any `!` line in an MRC
channel, in a channel whose bot policy is *none*, or in a channel no enabled
bot may use; elsewhere a channel without a default explains explicit naming.
The host strips terminal controls from request text, Linked or local, and
turns tabs into spaces before a bot sees it. `Baud.bot@Node: help` names one
remote instance, using Link's unique friendly name, DNS name or fingerprint
resolution. Unknown or ambiguous names are refused. Both nodes must carry
the Linked channel and advertise live bot support. Ordinary chat creates no
addressed request. A request token is
owned by that instance and expires after 60 seconds. There are at most 32
outstanding requests per instance. Busy, unavailable, expired and refused
requests give the caller an explanation.

A privately asked request is never recorded, broadcast, carried over Link as
a channel line, relayed to MRC or observed by channel readers: no other
person or bot learns of it. Its channel is the caller's current one, so the
bot must be allowed there and every grant, mute and channel policy applies as
for a public request; a refused one is explained to the caller.

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

Door source grants supply a door's game events (§16, issue #1378), which a
door reports through its outbound hook (door API 5; see the
[developer handbook](NetBBS-Developer-Handbook.md#game-events-and-standings)):

```json
{"type":"door","event":"opaque-event-token","channel":"lobby","source":"Voidrunner","item_id":"41","kind":"rank","title":"Merchant Captain","subjects":["Nib"],"text":"Nib reached Merchant Captain in Voidrunner."}
```

`kind` is `rank`, `achievement`, `medal`, `capture` or `perfect`; `subjects`
are the handles the host may name, in placement order for a `medal`; `text`
is the host's sentence, and `announce` publishes it. A door is a source only
while every caller may play it (minimum play level 0). The host leaves out a
caller who switched "Game results announced in chat" off, or whose account is
disabled, pending or the guest account, and does not offer an event naming
nobody; all of this, the door's own events switch and its level are checked
again at publication. Doors count toward the 32 sources, and their events
have their own journal of 1,024 references and their own cursor, with the
same startup skip, per-poll limit and expiry.

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
{"id":"8","action":"query","request":"opaque-token","query":"top","target":"voidrunner"}
{"id":"7","action":"announce","event":"opaque-event-token"}
```

`reply` publishes one line in the original request's channel; it is
**public** unless it carries `"private": true` (see
[Private answers](#private-answers)). `say` publishes in an allowed channel without requiring
a current request, for reminders. The host chooses the author identity. Both
actions store the line in ordinary chat history and apply the same policy,
mute and rate checks. Text must contain 1-500 characters, no line breaks, tabs
or terminal controls. Approved Linked output uses ordinary signed channel
events and live delivery; MRC never receives it. A remote request's public
reply is also delivered directly to its requesting node, so it can arrive
while the channel origin is offline. Normal live-message deduplication hides
any second copy through the origin. Reminders use the ordinary channel route.

### Lines held for a caller

`say.for` says a line for one caller (issue #1375). If that caller is in the
channel now, on this node or in a Linked channel's live roster, the host
publishes `text` at once. Otherwise it holds `held_text` (default `text`) and
publishes it in that channel, and only there, on the caller's next join of it:
once per join of the account, not once per session, and whether the caller is
away or not. `expires` is 60 seconds to 7 days.

```json
{"id":"9","action":"say.for","channel":"lobby","caller":7,"text":"alice: reminder #3: tea","held_text":"alice: reminder #3 (due 18:00, held until you came back): tea","expires":604800}
{"type":"result","id":"9","ok":true,"value":{"held":true,"held_id":12,"expires_at":"2026-10-25T16:00:00.000000Z"}}
{"type":"held","held_id":12,"outcome":"delivered","message_id":57}
```

A line published at once returns `{"delivered": true, "message_id": ...}`
instead. A held line's outcome arrives later as a `held` event, `delivered` or
`expired`, if the instance is running then; `held.list` returns the instance's
waiting lines (`held_id`, `channel`, `caller`, `created_at`, `expires_at`),
so a restarted bot can tell which were delivered meanwhile, and
`held.cancel` with a `held_id` withdraws one. A held publication obeys the
same grant, policy, mute and hourly-output checks as `say` when it is
delivered; a refused one stays held until it expires, and an expired one is
dropped without a line. An instance holds at most 48 lines, 8 per caller.
With `"private": true` the line is shown to that local caller alone, as a
`(to you)` answer in each of their sessions in the channel, and debits their
private-answer limit instead of the channel's hourly output (issue #1372); a
private line for a Link caller is refused. The `held` event then carries
`"private": true` instead of a `message_id`. Baud's reminders and tells are
delivered publicly.
The caller is any caller id the bot has seen; held lines live in the node
database, not in the bot's state, and survive restarts.

`tell` is the token-bound form (issue #1376): a note from the request's caller
to a local account, delivered publicly in the request's channel on that
account's next join of it:

```json
{"id":"10","action":"tell","request":"opaque-token","to":"Nib","text":"check the Retro board"}
{"type":"result","id":"10","ok":true,"value":{"held":true,"tell_id":4,"name":"Nib","expires_at":"..."}}
```

The host takes the sender from its pending request, never from the action, and
publishes the line itself: `Nib: Quill asked me to tell you (3h ago): check the
Retro board`. A recipient already in the channel gets nothing stored and the
value is `{"held": false, "present": true, "name": "Nib"}`. Recipients are
local accounts only, keyed by account, so a rename before delivery changes
nothing; `@` names are refused, and so is a recipient who could not enter the
channel. The recipient's direct-message opt-out and §6.4 block list apply,
for a local sender or a Link sender as `user@<fingerprint>`, when the tell is
made ("Nib does not accept messages from you") and again at delivery, where a
refused tell, or one from a Link node no longer allowed to author here, is
dropped silently. The text is 1-160 characters. At most 3 tells wait per
sender, 5 per recipient and 32 per instance, for at most 14 days, then are
dropped silently. A refused `tell` is shown to its caller alone, privately,
and consumes the token; it is not written to the SysOp's status line.
`tell.list` returns the request caller's own waiting tells in that channel
(`tell_id`, `to`, `text`, `created_at`), and `tell.cancel` with a `tell_id`
withdraws one of them; neither consumes the token.

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

### Private answers

```json
{"id":"8","action":"reply","request":"opaque-token","private":true,"text":"Saved reminder #4."}
{"type":"result","id":"8","ok":true,"value":{"private":true}}
```

A reply with `"private": true` answers the requesting caller alone. It
consumes the request token as a public reply does: one request gets one
answer, public or private. `private` defaults to how the request was asked,
so a privately asked request is answered privately even without the flag.
The text rules are a reply's: 1-500 characters, no line breaks, tabs or
terminal controls. The channel's grant, mutes and policy are checked again
before delivery; no other grant is needed.

The caller sees `[20:14] [BOT] <Baud.bot> (to you) Saved reminder #4.`: a
timestamped bot line, without a bell. It goes to the requesting session only;
if the caller has left the channel it is dropped, as an online-only `/msg`
is. It is never written to chat history, signed or relayed, and is not in
search or join replay.

Two refusals keep the token, so the bot can answer the other way: a public
reply to a privately asked request ("This request was asked privately;
answer it privately."), and a private answer the caller's node cannot take
(`private` false in the `message` event). A private answer is never made
public by the host.

A private answer never debits the channel's hourly output limit. Each
instance instead allows each caller at most 30 private answers an hour, a
fixed number retained across restarts. A privately asked request is checked
against that limit before it is dispatched; a public one is checked against
the channel's limit, since its answer may be public, and a private answer to
it debits the caller's limit instead. A spent limit is told to the caller.

### Titled blocks

```json
{"id":"9","action":"reply","request":"opaque-token","block":{"title":"Node stats","rows":[{"label":"Online","text":"4 people"},{"label":"Uptime","text":"3h 12m"}]}}
{"type":"result","id":"9","ok":true,"value":{"message_id":42}}
```

A `reply` may carry a `block` instead of `text`: an answer longer than a line,
drawn the way `/who` draws its rows. A block has a `title` of 1-40 characters
and 1-12 `rows`; each row has a `text` of 1-200 characters and an optional
`label` of up to 24, which is set in the label colour with the labels aligned,
the text wrapping under itself. Everything is plain text on one line, without
tabs or terminal controls; the bot sends no colour. A reply carries `text` or
a `block`, never both, and `say` and `announce` take no block: reminders and
announcements stay one line. A refused block is explained to the caller, as
any refused reply is.

A block may also say `"labels": "commands"`: its labels are command syntax,
and the host draws them the way `/help` draws its own commands. A label may
list several commands, separated by `, ` (`reminders, cancel <id>`); each
command's first word is set in the accent colour and bold, its parameters in
the label colour and the punctuation `< > [ ] |` and the separators muted.
`"commands"` is the only value; without the key the labels are plain. A host
of NetBBS 7.31 or older refuses a block with the key as an unknown shape, so check
the host's version before sending it to one.

```json
{"id":"10","action":"reply","request":"opaque-token","block":{"title":"Commands","labels":"commands","rows":[{"label":"remind <when> <text>","text":"a reminder here"},{"label":"reminders, cancel <id>","text":"your reminders; cancel one"}]}}
```

Callers see the host's label in front of the bot's title, with the time at
the right when they keep chat timestamps:

```
── [BOT] Baud.bot · Node stats ─────────────── 20:14 ──
 Online  4 people
 Uptime  3h 12m
```

The bot supplies only its own title, so a block can never claim another
author. A narrow terminal shortens the bot's title before the label, wraps
the last column under itself and cuts no row; a block taller than the chat
area is paged (`page 1 of 2 · PgDn more · PgUp back · Esc done`). Join replay
draws stored blocks whole, unpaged.

The host builds a plain fallback line from every block, `Title: label text;
label text`, cut to 500 characters: `Node stats: Online 4 people; Uptime 3h
12m`. That line is the message's body. Search matches it, older nodes show it,
and it is what the hourly limit counts: a block is one line, however many rows
it has. The bot never supplies a fallback of its own. A block is one chat
message, so mutes, `/bots local|none` and remote mutes hide it like any bot
line.

A block may also be a private answer, with `"private": true` or to a
privately asked request; it debits one private answer and its title ends
`to you`:

```
── [BOT] Baud.bot · Your reminders ──── to you · 20:14 ──
```

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
| `bot_request` | `channel_id`, `bot`, `caller`, `text`; optional `access`, `private` | Direct from the caller's authenticated home; the frame message id identifies the request. |
| `bot_result` | `request_id`, `error`; optional `private` | Direct from the expected bot host; empty error means its reply was published, or, with `private`, answered privately. |
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

The optional capability `chat_bot_private_v1` carries private requests and
answers. A `bot_request` may then carry `"private": true`, for a request its
caller asked privately, and a `bot_result` may carry
`"private": {"text": "..."}`, a private answer of 1-500 characters with an
empty `error`. Neither key is sent to a peer that does not advertise the
capability, and a peer that sends one without advertising it is refused as
malformed. A caller's node asks a host without it no private question; a host
gives no private answer to a caller whose node lacks it, and its bots see
`"private": false`. A private answer travels only in its `bot_result`, never
as a direct message or a channel event. The caller's node checks the remote
bot against its local mute and `/bots local|none` again on receipt, and drops
a hidden answer.

A titled block in a Linked channel is an optional `block` field in the signed
`channel_message` event, beside the fallback `body` the host built from it; a
node that predates blocks keeps the field and shows `body`. A receiving node
refuses an event whose block takes more than 12 KiB as JSON, and shows a
malformed block, or one on a line that is not a bot's, as its fallback line.
The optional capability `chat_bot_blocks_v1` carries the block live: as an
optional `block` key on the live `channel_message` frame, on a
`scrollback_snapshot` entry, and on a `bot_result`'s private answer
(`"private": {"text": "...", "block": {...}}`, its text the fallback line).
A block's `"labels": "commands"` travels inside the block in all of these. A
NetBBS 7.31 node takes the block's field and frame but does not know the key,
so it shows such a block as its fallback line; no capability marks it.
These frames are key-strict, so the key goes only to a peer advertising the
capability, and every other peer gets the fallback line alone. The public
reply to a remote request that goes directly to the caller's node carries the
same block, so live deduplication still sees one line.

```json
{"type":"result","id":"1","ok":true,"value":{"message_id":42}}
{"type":"result","id":"2","ok":false,"error":"Baud.bot is muted here."}
```

`query` requires a live request token belonging to that instance. The host
uses the caller and channel attached to the token, checks the current query
grant and channel policy, and rechecks the local account or remote Link trust.
A successful query returns an object in `value`, including a public-safe
`text` suitable for a reply. Query success or refusal does not consume the
token; the bot can still send its one `reply`, public or private. No arbitrary database
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
- `top`: a door's standings, the top ten the door last reported. `target` is
  a door name, matched without case, or a unique prefix of one. An available
  result has `available: true`, `door`, `title`, `updated_at` and `rows`, each
  `{"rank", "name", "value", "label"}`. Rows for callers who switched "Game
  results announced in chat" off, or whose accounts are disabled, pending or
  the guest account, are left out and the rest renumbered from 1. Only doors
  every caller may play, with game events on, answer. An empty or unmatched
  `target` returns `available: false`, `doors` (the doors with standings) and
  a `text` naming them.

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
channel (default 60), retained across restarts; private answers have their own
fixed limit of 30 per caller per hour per bot. Node limits are 16 instances
and 32 allowed channels each. Exit/failure restarts with exponential backoff
from one to 60 seconds. Kill controls disable the instance persistently.

## Bundled Baud

Commands: `help`, `remind <when> <text>`, `reminders`, `cancel <id>`,
`tell <name> <text>`, `tells` and `cancel tell <id>`; `help remind` and
`help tell` explain the two. `roll` and `coin`, and, when switched on,
`quote` and `poll`/`vote` (below).
On a host that takes blocks, `help` answers with a titled block, `Commands`,
one row per command and the help opening (below) as its first row, its labels
marked as command syntax (`"labels": "commands"`); an older host gets the same
list as one line.
`sysop` only points to the host's `/sysop <reason>` command: by the time Baud
sees a `!` line it has already gone to the channel, so Baud cannot page anyone
and says so. `help` mentions `/sysop` too. No bot can page the SysOp: a page
comes only from a caller's own `/sysop` (decision 1377).
With the corresponding grants, Baud also offers `stats`,
`seen <local-account>` and `top <door>` (a door's standings) and lists them in
`help`.

Baud answers each request the way it was asked: a `!` line or `Baud.bot:`
line publicly, with the caller's name in front, and `/msg Baud.bot <text>` or
a `/private Baud.bot` line privately. Some answers are private whenever the
`message` event's `private` says a private answer can reach the caller, even
when the request was asked in the channel: `help`, `help remind` and
`help tell`, with `help` ending "Ask privately: /msg Baud.bot <text>."
(`Baud.bot@Node` for a Link caller), and `reminders`, which then lists all
of the caller's reminders across channels as `#1 lobby: check the oven`.
`cancel <id>` then reaches a reminder in any of the caller's channels, so
every id that listing shows can be cancelled where it was read. Only a caller
whose node cannot take a private answer gets these publicly; that `help`
ends "Replies are public.", and that `reminders` and `cancel` keep to the
current channel.

With **Grant model conversation**, other addressed text requests the host's
model service; `reset` clears this caller's temporary context here. Ordinary
commands stay local and remain responsive during a provider request.
The separate **Baud greetings** toggle greets live joins in presence-event
channels. Greetings obey the same mutes, channel policy and hourly output limit
as replies and reminders, and require no whole-channel reading grant.
Post, file and door announcements likewise need only their explicit source
grants and destination, with the corresponding Baud ability enabled. They require no
channel reading or presence access.
Reminder text is capped at 160 characters. Baud holds up to eight reminders
per caller and 48 per instance, also subject to the state byte limit. A
public listing works only for the caller's reminders in the current channel,
so a public answer cannot expose a reminder from a private channel. A reminder is said publicly in its channel when due, however it was
set.

`<when>` is a duration (`10m`, `2h`, `1s` to `30d`) or a time in the node's
display clock, from the hello's `timezone`: `HH:MM` (today, or tomorrow once
it has passed), `tomorrow HH:MM`, `mon` to `sun HH:MM` (the next such day,
today while the time is ahead) or `YYYY-MM-DD HH:MM` (refused once past).
Times are 24-hour and dates ISO only: `01.11` could be either order and is
refused. A time in a spring-forward gap is read at its pre-transition offset
and a repeated one at its first occurrence, as the backup schedule does; at
most 30 days ahead. Due times are stored as UTC instants, so a change of the
display zone never moves a saved reminder. The confirmation names the instant
in the node clock: `Saved reminder #3 for Fri 20:00 CET.`

A due reminder is said with `say.for` for its owner. If they are not in its
channel, the host holds it for 7 days and says it on their return, as
`alice: reminder #3 (due 18:00, held until you came back): net meeting`;
after that it is dropped silently. `reminders` lists a held one as `waiting
since 18:00`, cancelling it withdraws the held line, and held reminders still
count toward the eight and the 48. After a restart Baud asks `held.list` which
of its held reminders are still waiting.

`tell` sends the host's token-bound `tell` action, so tells live in the node,
not in Baud's state. Baud confirms where the note will be said, publicly
unless the caller asked privately, or answers `Nib is here now.`; a refusal is shown to the caller alone. `tells`
lists the caller's own waiting tells in this channel, and `cancel tell <id>`
withdraws one.

Due reminders wait through shutdowns. A refused reminder backs off on its own
(one minute, doubling to an hour) while other due reminders are still
delivered, and is dropped after eight refusals. They are removed after
successful publication; a crash between publication and saving that removal
can repeat one. This is not an exactly-once delivery guarantee. If a channel
is renamed or a grant removed, old reminders retain their original
destination, cannot migrate to another channel automatically, and are dropped
once their refusals run out.

### Dice, coin, quotes and quick polls

`roll` is `1d6`; `roll NdS`, `roll dS` and an optional `+M` or `-M` (any case)
roll N dice of S sides, N 1-20, S 2-1000, M 0-1000. The public reply lists each
die and the total: `alice: rolled 2d6+1: 3 + 5 + 1 = 9.` Anything else gets the
usage. `coin` answers `flips a coin: heads.` or `tails.` Both draw from
`random.SystemRandom` and are always on.

**Baud quotes** (off by default) keeps per-channel quotes: `quote add <text>`
(1-160 characters), `quote` for a random one, `quote <n>` and `quote del <n>`.
A quote shows who added it, `#4 "<Quill> the modem is on fire again" (added by
Nib)`, and nothing else: a name inside the text is the adder's claim. Quotes
are shown and deleted only in their channel. The adder or a channel moderator
deletes one; a moderator's deletion of someone else's quote is said in the
channel (`Quote #4 deleted by Ann.`), which is its record. Guests cannot add
quotes. At most 30 per instance and 10 per adder.

**Baud quick polls** (off by default): `poll [<1m-24h>] <question> | <a> | <b>
...` opens a single-choice poll of 2-6 answers (up to 30 characters each, the
question up to 100) for 10 minutes unless a length is given. One poll is open
per channel and at most 4 per instance; guests may vote but cannot open one.
`vote <n>` counts one vote per caller id, changeable until close, and is
acknowledged privately (publicly to a caller whose node cannot take a private
answer). `poll` alone shows the question and the number of voters, never the
tally. The results are said once, at close: `Poll #3 closed: Best door?
Voidrunner 4 (50%), War Dialer 3 (38%), Trivia 1 (13%). 8 voters.` The opener
or a moderator closes a poll early with `poll close`. Either way it closes on a
tick, with the reminder backoff and retries, survives restarts, and is removed
once said. At most 100 voters are counted; the result says how many were not.
The `!vote` line itself is public chat, and the opening line says so.

In a Linked channel the opening line adds `or from another BBS:
Baud.bot@<node>: vote <n>`, because a `!vote` there reaches the caller's own
node's default bot. Baud on that node answers `No poll is open here.` with a
pointer to the explicit form, even with its own polls switched off.

Quotes and polls share the instance's one 16 KiB state value with reminders.
A save the host refuses is answered with the `full` phrase, `Baud's memory is
full; ask the SysOp.` Dice spam spends the channel's lines per hour like any
reply and can delay a reminder or a poll's close; `/mute` and `/bots none` are
the remedy.

Its temperament sets five fixed phrases, and the SysOp may override any of
them per instance under **Phrases** in the instance's editor (they arrive in
the hello's `phrases`):

| Key | Used for | Friendly preset |
| --- | --- | --- |
| `greeting` | A live join, with greetings on | `Welcome, {name}!` |
| `help` | The opening of the `help` reply, its block's first row | `Happy to help!` |
| `busy` | The reply while a full queue waits on a reminder save | `Busy saving reminders; try again shortly.` |
| `limit` | The reply at the reminder limit, held reminders included | `Reminder limit reached (8 per caller, 48 per bot). Cancel an old reminder first.` |
| `full` | A quote, poll or vote whose save was refused | `Baud's memory is full; ask the SysOp.` |

Terse greets with `Welcome, {name}.` and has no help opening; retro says
`Carrier detected: welcome, {name}!` and `Carrier detected.`. An empty override
falls back to the preset, and a phrase that does not format is ignored the
same way rather than stopping Baud. Command syntax and the help list itself
are not phrases and stay as they are.

Baud's standalone implementation in `src/netbbs/bots/baud.py` is an API example
written on the helper below: it uses standard input/output only and has no
database or filesystem access.

## Writing a bot

### The helper: `netbbs.bots.botkit`

`src/netbbs/bots/botkit.py` is one standard-library file that owns the
protocol plumbing, so a bot is only its own handlers. It imports nothing from
NetBBS: import it as `netbbs.bots.botkit` where NetBBS is installed, or copy
the file next to your bot and `import botkit` under any Python 3.9 or later.

```python
from botkit import Bot, run

class Echo(Bot):
    def on_message(self, msg):
        msg.reply("you said " + msg.text)

run(Echo)
```

What it does for you:

- **Handshake.** `run(BotClass)` reads the `hello`, builds your bot from it and
  answers `ready`. It speaks exactly one version, `botkit.API_VERSION`, which
  equals the host's `BOT_API_VERSION`; any other `hello` version is refused
  with a line on stderr and exit status 1, and no `ready` is sent. Use the
  copy that ships with the node you run on.
- **Dispatch by event type.** An event calls `on_<type>`: `on_message`,
  `on_channel`, `on_presence`, `on_tick`, `on_post`, `on_file`, `on_held`, `on_door`. A type without
  a handler, including one a newer host adds, is ignored. Fields read as keys
  or attributes (`event.channel`, `event["channel"]`); a missing one reads as
  `None`.
- **Grants.** The bot's attributes `queries`, `reading_channels`,
  `event_channels`, `board_sources`, `file_sources`, `door_sources`,
  `announcement_channel`, `greetings`, `announce_posts`, `announce_files`,
  `announce_doors` and `model_conversation` are
  read from the hello's grant lists. `scope` is always `addressed` and says
  nothing about the other grants.
- **Actions and results.** Every action goes through `bot.act(action, then,
  **fields)`, which gives it an id and, when `then` is given, calls
  `then(result)` when its result arrives. Any number may be outstanding, and
  events still arrive in between. The shortcuts are `bot.say(channel, text)`,
  `bot.announce(event)`, `bot.say_for(channel, caller, text, held_text,
  expires)`, `bot.held_lines(then)`, `bot.cancel_held(held_id)`,
  `bot.load(then)` (`state.get`) and `bot.save(value)`, and on a message
  `msg.reply(text)`, `msg.reply_block(title, rows)`, `msg.query(name, target)`,
  `msg.model()`, `msg.model_reset()`, `msg.tell(to, text)`, `msg.tells()` and
  `msg.cancel_tell(tell_id)`. Each takes an optional `then`:

  ```python
  msg.query("stats", then=lambda result: msg.reply(result.text))
  ```

  A `Result` has `ok`, `value` and `error`; `result.text` is a query's text,
  or the reason the action was refused.
- **Replies.** `msg.reply(text)` puts the caller's name in front and cuts the
  line to 500 characters, as Baud does. `say` is cut the same way.
- **Private answers.** `msg.reply(text, private=True)` answers the caller
  alone (see [Private answers](#private-answers)), without their name in
  front. `msg.private` is true when a private answer can reach this caller,
  and `msg.asked_privately` when they asked with `/msg` or `/private`; both
  read false from a host that does not send them. Left out, `private`
  follows how the request was asked, so a privately asked request is
  answered privately and a public one publicly. `msg.model(private=True)`
  and `msg.model_reset(private=True)` take the same flag.

  ```python
  def on_message(self, msg):
      if msg.text == "mine":
          msg.reply("only you see this", private=msg.private)
  ```

  With `private=msg.private`, a caller whose node cannot take a private
  answer gets a public one instead of a refusal.
- **Blocks.** `msg.reply_block(title, rows)` answers with a titled block; a
  row is a `(label, text)` pair, or a plain text for a row without a label.
  It cuts the title, the rows, each label and each text to the block's bounds,
  and puts no caller name in front: the host titles the block with the bot's
  name. `private=True` sends it as a private answer. `labels="commands"`
  marks the labels as command syntax (see [Titled blocks](#titled-blocks)).
  `bot.blocks` says whether the host takes blocks; answer with `msg.reply`
  when it does not.
- **State.** `bot.state` starts as the hello's `state` and becomes a saved
  value once the host has stored it; a refused save leaves it unchanged.
  `bot.change(step)` runs `step()` once every save already sent has its
  result. Put a command that reads the state and saves a new one through it,
  so two such commands never build on the same stale copy. It returns `False`
  without running `step` when 32 are already waiting; answer that caller with
  a "busy" line. `bot.saving` is true while a save or a change is waiting.
- **Flushing.** Every line is written as ASCII JSON and flushed.

Handlers are plain synchronous code; a wait is a callback, and two waits in a
row nest. An exception ends the process, and the host restarts it with
backoff; in a trial its traceback shows in the stderr excerpt.

### The second example: Tally

`src/netbbs/bots/tally.py` counts karma from whole-channel reading. It ships
installed and off; no preset creates it. Enter it under **Program arguments**
with the node's interpreter, for example
`["/usr/pkg/bin/python3.11", "-u", "-m", "netbbs.bots.tally"]`, or by file name
from a source checkout, and grant **Read whole channels** for the channels it
should count. Callers see that grant in the channel heading and in `/who`.

```
[20:14] <alice> thanks bob++ for the file fix
[20:14] [BOT] <Tally.bot> bob now has 4 karma.
[20:15] <carol> !karma bob
[20:15] [BOT] <Tally.bot> carol: bob has 4 karma (top: bob 4, alice 2).
```

It shows what Baud does not. A `channel` observation carries no request
token, so there is no caller to reply to: Tally `say`s its line to the
channel. `!karma [name]` is an addressed request and is answered with
`reply`. Its own state holds at most 200 names, well under the 16 KiB limit,
and a refused save is reported on stderr and said nowhere. A caller cannot
give karma to themselves, and at most five names count per line.

### Try a bot in the console

The bot editor's **Try** field runs the draft as it stands, unsaved, and the
Chat bots list's `[T]ry selected` runs the saved instance. Neither asks a
question first: a trial changes nothing on the node. See the
[SysOp handbook](NetBBS-SysOp-Handbook.md#try-a-bot) for the screen.

A trial runs the real host runtime against a scratch database, state
directory and channel in a temporary directory, so every check (addressing,
grants, the 500-character line, state and rate limits, request tokens)
applies as it will live. It never touches the node's database, `.bots`
directory, rate history or chat, and live callers never see it. The channels,
boards and file areas the grants name exist in the scratch node by the same
names; Linked approvals do not apply there. Model actions are rehearsed: the
result and the caller's notice say what would have been asked, and nothing is
sent to the provider. Stderr is kept to its last 8 KiB and shown; live, it is
discarded. There is no command-line front end.

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
are not used: in help-desk mode the host supplies node facts itself.
Redirects are refused, responses are capped at 64 KiB and requests time out
after 40 seconds. At most two model requests run on a node at once.

External programs use the same actions as Baud:

```json
{"id":"7","action":"model","request":"opaque-token"}
{"id":"8","action":"model.reset","request":"opaque-token"}
```

These return an immediate `result` with `value: {"pending": true}`. The host
then publishes the reply or sends a refusal to the requesting caller. Like
`reply`, both take an optional `"private": true`, defaulting to how the request
was asked; the answer, the disclosure and the reset notice are then private,
under the same refusals and limits as a private reply. The
request token fixes the original input, caller and channel: program-supplied
prompts, identities or destinations cannot substitute them. Observations and
source-event tokens cannot request a model. API keys stay in the host; they
never enter the program's handshake, environment or result. Installed programs
remain trusted OS-user code, as described above.

The first conversational request only publishes a notice naming the provider,
what is sent, the context allowance and prompt-logging policy. In help-desk
mode it reads: "Sends your addressed text + up to N prior lines of only your
exchange here, handbook excerpts and node facts", and for a private answer
adds "incl. boards and areas you can see". It is not forwarded or added to
context. Submit again to proceed. Disclosure repeats after settings changes,
context expiry, reset or restart. Model input consists of the host's configured
persona with the active bot's chat identity, this addressed message and at most
0-20 prior lines of this caller's exchange with this bot in this channel. It
never includes ordinary conversation
or another caller's exchange, even if the bot has a whole-channel reading grant.
Only delivered replies, published or private, enter context. Each caller, bot
and channel has one context for public answers and another for private ones,
each with its own disclosure and reset: a private answer may say what a public
one must not, so it never resurfaces through a later public answer. Context is
memory-only, limited to 256 entries, expires after 30 idle minutes, and
clears on bot restart. Eviction requires a new disclosure. Reset does not erase
channel history, existing backups, optional prompt logs or provider records.
Replies are sanitized and capped at the configured 80-500 characters.
Disclosure and reset notices have their own fixed wording and the ordinary
500-character chat limit.

**Modes.** Model settings choose **Help desk** (the default; an absent
setting reads as help desk) or **Companion**, each with its own shipped default
persona. Companion sends only the input above. Help desk adds host-chosen
grounding to the system message, never to the prior lines:

- up to three sections of the caller handbook, which ships with NetBBS as
  package data, picked per request by a standard-library keyword score under
  a fixed 4,000-byte cap;
- node facts, under 1,500 bytes: name, tagline, SysOps, which of MRC, browser
  links, doors and FTN networks the node has, and its boards and file areas.
  A public answer, and any answer to a Link caller, names only resources
  readable by everyone without age or name requirements, Community defaults
  included. A private answer to a local caller names the boards and areas that
  caller can see. Hidden Communities are left out for non-SysOps;
- the `stats` answer, when the bot holds that query grant, through the same
  query path and caller check as the `query` action;
- fixed rules: name only menus, keys and commands found in the grounding, and
  otherwise say so and point to `[?] Help`.

The host then appends the heading of the best excerpt and a hint to the reply,
within the reply cap: `(Handbook: Exchange files. May be wrong; ? for help)`.
Context keeps only the model's own words. Grounding bytes count toward each
reservation like any other input.

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

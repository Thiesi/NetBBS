# NetBBS Developer Handbook

This handbook has three entry points: **write a door**, **integrate NetBBS Link**,
or **contribute to NetBBS**. It describes the current implementation and points
to the exact source, fixtures, and normative decisions needed to go further.

[All documentation](README.md) · [User handbook](NetBBS-User-Handbook.md) ·
[SysOp handbook](NetBBS-SysOp-Handbook.md)

## Contents

- [Choose your integration boundary](#choose-your-integration-boundary)
- [Set up a development checkout](#set-up-a-development-checkout)
- [Developing a native door](#developing-a-native-door)
- [Door launch metadata](#door-launch-metadata)
- [Door outbound posting](#door-outbound-posting)
- [Implementing NetBBS Link](#implementing-netbbs-link)
- [Two-node Link exercise](#two-node-link-exercise)
- [Contributing to NetBBS](#contributing-to-netbbs)
- [Node configuration reference](#node-configuration-reference)
- [Validation and release work](#validation-and-release-work)

## Choose your integration boundary

| Target | Interface | Starting reference |
| --- | --- | --- |
| A game or interactive external application | Supervised process, terminal streams, launch JSON, optional classic drop files | [Door runtime](../src/netbbs/doors/runtime.py), [Retro Trivia](../src/netbbs/doors/bundled/retro_trivia.py) |
| A door posting results to a message board | SysOp-enabled file-drop hook, door-specific label and allowlist | [Outbound implementation](../src/netbbs/doors/outbound.py) |
| Another implementation of a NetBBS Link node | Signed events, authenticated HTTP operations, separate Noise real-time transport | [Events](../src/netbbs/link/events.py), [protocol](../src/netbbs/link/protocol.py), [transport](../src/netbbs/link/transport.py) |
| A change inside NetBBS | Domain functions and owned session/network flows | [Architecture and product design](NetBBS-design-doc.md), [engineering record](NetBBS-worklog.md) |

NetBBS Link is a peer protocol, not a general anonymous REST API for driving
a BBS. A door does not need to implement Link merely to publish game results;
use the outbound hook. NetBBS has no general external plugin API granting
arbitrary access to accounts, private mail, or administration.

Current compatibility identifiers are **door API 3**, durable **Link envelope
version 1**, and **real-time Link protocol version 4**. They version different
contracts; none is interchangeable with the package version. Link v1 is still
a pre-freeze protocol. Independent interoperability is unclaimed pending
[issue #71](https://github.com/Thiesi/NetBBS/issues/71).

## Set up a development checkout

Use Python 3.11+; NetBSD is the production design target and mainstream Linux
is supported. Windows is useful for development but does not exercise POSIX
signals, PTYs, descriptor inheritance, or production file permissions.

From a source checkout:

```sh
git clone https://github.com/Thiesi/NetBBS.git
cd NetBBS
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python -m netbbs --version
```

On Windows PowerShell, create the environment with `py -m venv .venv`, then
use `.venv\Scripts\python.exe` for the commands instead of activation. The
`dev` extra includes pytest, the SSH dependency, and aiohttp. It is not
required for an operator installation.

Keep test state separate from a real node. To explore the UI locally, create
a disposable database through `python -m netbbs.admin --db /absolute/test/netbbs.db`,
then start `python -m netbbs` with a test TOML file. Set distinct database,
identity, and game-save paths and bind listeners to loopback. Use the
[SysOp handbook](NetBBS-SysOp-Handbook.md#first-account-and-configuration)
for the bootstrap procedure. Do not use production credentials or identity
directories for experiments.

## Developing a native door

### First exercise

1. Read [Retro Trivia](../src/netbbs/doors/bundled/retro_trivia.py): its
   `_load_door_info`, input helpers, `out_line`/`out_prompt`, and main loop
   show a complete stdlib-only application. Run the installed example with
   `python -m netbbs.doors.bundled.retro_trivia` in a real terminal.
2. Register that bundled example through **SysOp → Content → Doors → Gallery**
   and play it over the caller transports you intend to support.
3. For your own program, use the **native-stdio** or **native-python-module**
   template. Set the absolute interpreter/executable, argv, and persistent
   installation directory. Keep minimum play level 255 until tested.
4. Read the launch JSON named by `NETBBS_DOOR_INFO`, select a supported API
   version, and honor the published geometry and display preferences.
5. Test normal quit, timeout, disconnect, repeated launches, and save recovery
   before opening the registration to callers.

See [the installation guide](NetBBS-door-guide.md#native-doors) for distributing
a packaged door to a SysOp. A separate game virtual environment avoids letting
your dependencies change the BBS environment.

### Input, output, and process lifetime

With the native stdio profile, standard input and output carry the caller's
terminal stream. Flush prompts; a buffered prompt the caller cannot see
cannot receive an answer. Handle end-of-input and broken output promptly.
Standard error is diagnostic output, not text shown to callers; the SysOp
can inspect a bounded recent excerpt.

Default encoding is UTF-8; CP437 translation is available for legacy profiles.
Do not assume ASCII byte count equals displayed width. Sanitize untrusted text
before adding ANSI, wrap by display columns, and use the reported dimensions.
Do not interpret the presence of a color capability as a reason to make
meaning depend on color alone.

For POSIX programs requiring a controlling terminal, choose the **PTY**
endpoint. For a socket-aware DOOR32 program, choose **socketpair**; the inherited
descriptor is private to the door bridge, not the caller's connection.
A Linux executable is not a NetBSD executable, and a POSIX descriptor is not
a Windows Winsock handle.

NetBBS launches an argv vector without a shell. Profile arguments support
`{node_dir}`, `{install_dir}`, `{node}`, `{door_sys}`, and `{door32}`;
use doubled braces for literal braces. Shell pipelines, redirection, or
variable expansion in an argument do not become shell operations.

Unprofiled doors start in a disposable working directory. A native profile with
`install_dir` uses that persistent directory as its working directory, while
metadata and drop files remain in a separate disposable launch directory.
Never infer the metadata path from the current working directory.

The child inherits a deliberately narrow environment, plus approved profile
environment entries. Native profiles also receive `NETBBS_DOOR_NODE` and
`NETBBS_DOOR_NODE_DIR`; a profile supplies `TERM=ansi` unless overridden.
Use absolute interpreter and data paths rather than depending on a login shell.

Default resource limits include 300 CPU seconds, 256 MiB address space,
a 3,600-second wall-clock limit, and a five-second stop grace. Profile settings
may change them; a zero CPU/wall limit removes that particular ceiling subject
to the host's hard limits. CPU time is not elapsed session time. The two
ceilings end a run differently: the wall-clock limit sends `SIGTERM` and waits
out the stop grace, while the CPU limit is `SIGKILL` with no warning, because
the soft and hard limits are set equal and no `SIGXCPU` precedes it. A door
cannot save at the CPU ceiling; a door that must never be cut that way runs
with the ceiling raised or removed, and with no hard CPU limit on the service
itself, since a zero profile value only lifts the door to whatever hard limit
the service inherited.
The POSIX process-count limit applies to the real UID, not just one door.
On shutdown/disconnect/timeout, NetBBS terminates and reaps owned processes,
escalating after the grace period. Save important progress during play, not
only at normal exit.

**Trust boundary:** a native door runs under the service account and can access
that account's files and network. The subprocess model and resource ceilings
are not a filesystem sandbox. Do not document a private temporary directory
as protection for the node's keys or database.

### Persistence and concurrency

Keep persistent data outside the temporary launch directory. For a profiled
third-party door, the installation directory is the usual root; document any
additional paths and how to back them up. Identify a player using the stable
`user_id` together with `node_id` if data can be shared between nodes.
Handles and display names can change. These fields are identifiers, not
credentials or evidence of user privileges.

Write saves atomically and validate them before replacing a usable generation.
Use appropriate locking or transactions for shared state. The profile's
`max_sessions` controls admission; raising it does not implement concurrent
game logic. Explain to the SysOp whether a world is per node, per player,
or shared across installations.

Voidrunner demonstrates persistent per-player careers and recovery; War Dialer
demonstrates a shared SQLite world. Both are considerably larger examples than
Retro Trivia. Their established save/ownership formats are not a generic
door API to reuse blindly. See the [game operations reference](NetBBS-door-guide.md)
for their maintenance and backup boundaries.

### Resize and browser behavior

For a native profile following the caller's geometry, PTYs receive terminal
size changes as `SIGWINCH`, and a single resize may deliver it more than once
(the kernel's own signal to the foreground group plus NetBBS's explicit one to
the process group), so a handler must be idempotent: read the current size and
redraw rather than count signals. Native stdio/socket profiles opt in with
`resize_signal`; NetBBS republishes the JSON dimensions atomically and sends
`SIGUSR1` on POSIX. Read the file again after notification and redraw. A
fixed-width profile,
a DOS profile, or an unprofiled launch does not acquire dynamic resizing
merely because the JSON contains dimensions.

The browser transport enters an explicit bounded raw door mode and restores
ordinary BBS mode on return. Send terminal bytes through the door endpoint;
a door should not manipulate the browser's NetBBS control protocol.
Check text decoding, control keys, backspace, and resizing in an actual browser
as well as a terminal client.

### Companion service and distribution

A profile can declare one long-lived companion service using the door's
executable and its own argv. It runs in `install_dir`, starts with the node
or on the first caller, and has PID or Unix-socket health checking.
Its memory/stop-grace settings are separate from a caller's limits; it has no
per-session CPU-seconds ceiling. NetBBS bounds restart attempts and exposes
service controls and diagnostics to the SysOp.

Ship a profile JSON alongside your package. The Compatibility screen accepts a
complete template (`executable_path`, `args`, `profile`) or a bare profile.
Use the installed [presets](../src/netbbs/doors/presets) and
[profile parser](../src/netbbs/doors/profiles.py) as the schema reference.
Document all path substitutions, installation permissions, runtime requirements,
service readiness, save locations, and safe backup/shutdown procedures.
See [companion-service setup](NetBBS-door-guide.md#doors-with-a-companion-service)
for the full profile shape and operator controls.

## Door launch metadata

### `door_info.json`

Every **native** door -- stdio, PTY or socket -- is given the path to a small
JSON file in `NETBBS_DOOR_INFO`. It is the NetBBS-native alternative to the
classic drop files, and such a door may use either or both.

Two kinds of registration deliberately get none of it:

- A **DOS** door. `NETBBS_DOOR_INFO` is set in the emulator's *host*
  environment and names a host path; nothing inside the guest sets it, and a
  DOS path could not reach it anyway. Give a DOS game the classic
  [drop files](NetBBS-door-guide.md#register-and-test-inside-netbbs) instead — which is what the
  DOS templates configure, and what a DOS-era program can actually read.
- A **remote** (RLogin) registration. NetBBS launches no process for it, so
  there is no environment to carry a path and no filesystem in common. The
  RFC 1282 handshake conveys only the configured local and remote identity
  strings and a terminal type; everything else about that caller stays on
  this side of the connection.

| Field | Meaning |
| --- | --- |
| `door_api` | Contract version, currently `3`. Refuse a version you do not understand rather than probing for fields. |
| `handle` | The caller's NetBBS handle. |
| `user_id` | Their stable numeric id on this node. |
| `terminal_width`, `terminal_height` | Current geometry; rewritten mid-run if the caller resizes and the door opted in (see the resize section above). |
| `color_depth` | `truecolor` or `256`. |
| `unicode_style` | The caller's own NetBBS glyph preference, so a door can match what they already chose. |
| `transport` | `telnet`, `ssh`, `web`, `local`, or `unknown`. Key decoding and latency assumptions differ, particularly for the browser terminal. |
| `timezone` | The node's display timezone as an IANA name, for in-game clocks. Node-wide: NetBBS has no per-caller timezone. |
| `node_name` | The node's display name, which a SysOp may change at any time. |
| `node_id` | A stable, opaque per-node identifier which survives a rename. Key a door's world on this, not on `node_name`. Not a credential. |
| `session_limit_seconds` | The effective wall-clock cap for *this* launch — the tighter of the profile's limit and any lower bound the launch itself imposes — so a door can warn before it is cut off. Absent when nothing bounds the run. |
| `outbound` | Present **only** if a SysOp switched this door's outbound hook on: `label` (the name its posts appear under), `directory` (where to drop a request, relative to the file's own directory), `results` (absolute path where outcomes are kept across launches), `boards` (every board it may name) and `posts_per_hour`. See [Door outbound posting](#door-outbound-posting). |

Treat every field as optional and absence as "unknown": that is how the file
stays compatible as it grows. Two notes on what is deliberately **not** there.
`node_fingerprint` (the Link identity) is not published yet — the node's own
identity is not held in the database, so supplying it would mean threading it
into the door runtime; `node_id` is what a door keying its world on the node
needs today. And nothing here is a credential or a privilege: no password, no
email, no user level, no IP address. A door learns who the caller says they
are, not what they may do.

The version field is the contract discriminator. For supported versions, allow
documented optional fields to be absent and ignore unknown additive fields.
Require whichever identity or capability fields your own feature actually
needs, and fail clearly if they are unavailable. The implementation currently
publishes `unicode_style` as a boolean.

The bundled War Dialer may additionally receive `war_dialer_owner`; it is a
private world-ownership convention, not a general authentication facility.

## Door outbound posting

The hook is opt-in per registered door and publishes under a persistent,
distinct door label. The playing caller does not lend their account or level
to the request. The SysOp chooses permitted boards and a per-door hourly
ceiling; the default is six posts. Normal board moderation still applies.

A live node with Link identity can queue a successful linked-board post for
federation. A standalone admin launch has no live Link context. SysOp
compatibility tests are rehearsals and do not publish outbound requests.
This interface currently posts to message boards; it does not send chat,
private mail, or arbitrary administrative operations.

### Request and result contract

A door learns about its hook from `door_info.json` (see
[Door launch metadata](#door-launch-metadata)). The `outbound`
key is **absent** when the hook is off, which is the only supported way to
test for it:

```json
"outbound": {
  "label": "Blacksite.door",
  "directory": "outbound",
  "results": "/home/netbbs/.netbbs/door-outbound/3",
  "boards": ["Chronicle"],
  "posts_per_hour": 6
}
```

If `"rehearsal": true` is present, a SysOp is *testing* this door rather than
a caller playing it. The drop directory works exactly as it always does, so
your posting path is exercised, but nothing written is published and no result
comes back. Say so rather than reporting a post you did not make.

`directory` is relative to the directory holding `door_info.json`, and NetBBS
has already created it. To post, write one JSON file there:

```json
{"board": "Chronicle", "subject": "Season 1 closes", "body": "..."}
```

- Write it under a temporary name and **rename it into place** with a `.json`
  extension. NetBBS ignores anything not ending in `.json`, so a half-written
  file is never read. The check is case-insensitive, so `POST.JSON` counts.
  Use your language's atomic rename — `Path.replace` in Python, `rename(2)`
  in C.
- Keep a request under about 216 KB. A larger one is refused unread rather
  than loaded into the BBS process.
- `board` may be omitted if exactly one board is allowlisted. With more than
  one, a request that names none is refused rather than guessed at.
- `subject` must be non-empty; `body` may be empty.

Requests are processed when the door exits. For each one, NetBBS removes the
request and writes a result into the directory named by `outbound.results` —
an absolute path outside the working directory, because the working directory
is deleted the moment the run ends and a result left there could never be read
by anyone.

Result files are named `<launch>.<your request name>.result.json`, where
`<launch>` differs for every run. Do not construct that name: read the
directory, parse each file, and match on the `request` field it carries. This
is what lets two sessions of the same door run at once without one
overwriting the other's outcome — which matters for any door that permits
more than one player at a time.

```json
{"status": "posted", "post_id": "...", "board": "Chronicle", "moderated": true,
 "request": "chronicle", "at": "2026-09-12T18:04:11.502133Z"}
{"status": "rejected", "reason": "board 'Private' is not allowlisted for this door",
 "request": "chronicle", "at": "2026-09-12T18:04:11.502133Z"}
```

If a SysOp switched the hook off while your door was running, its requests are
simply dropped and no result is written — there would be nowhere you could
find one, since the next launch has no `outbound` block at all. That absent
block is how you learn the hook is off.

A refusal is never queued for later — a post held back and published after a
SysOp revoked the allowlist is the surprise the switch exists to prevent. Your
door can read the result on its next launch if it wants to know what happened;
`"moderated": true` means the post is waiting for the SysOp's approval rather
than already visible.

Reasons you can expect to see, and what they mean for the door:

| `reason` contains | What happened |
| --- | --- |
| `not switched on` | The SysOp has not enabled outbound. Stop trying. |
| `no board is allowlisted` | Enabled, but nothing is allowed yet. |
| `not allowlisted for this door` | The board name is wrong, or was revoked. |
| `matches more than one` | Two allowlisted boards differ only by case; spell one exactly. |
| `more than one allowlisted board` | Name a board in the request. |
| `rate limit reached` | Try again later; the ceiling is in `posts_per_hour`. |
| `larger than` | The request exceeded the size limit and was not read. |
| `requests in one session` | You wrote more in one session than a drain answers. |
| `switch it on again` | The account that enabled the hook is gone, so it has lapsed until a SysOp vouches for the door again. |

Results are bounded receipts, not an indefinitely retained event stream. A
missing receipt is not proof of publication or rejection. Persist your own
request identifier in your game data and choose request filenames accordingly.
Do not promise exactly-once delivery after a crash or retry merely because
result files have unique launch prefixes.

Backup and restore carry receipts with the node. A restore replaces the
receipts on disk with the ones belonging to the database it restores, so a
receipt never survives into a generation that never issued its post, and a
`"posted"` receipt in an archive names a post that archive's database snapshot
contains — unless the post had since been deleted, which a running node shows you
just the same (deleting a board takes its posts and leaves the receipts). A
restored node can also hold a published post whose receipt is missing, the same
case as a pruned receipt. Neither direction is proof: a missing receipt is not
grounds on its own for publishing again, and a receipt is a record of what was
published, not of what is still there.

Name your requests as ordinary filenames. A receipt is named after the request
it answers, so a request name holding a path separator — legal on the POSIX
target — makes a receipt that is one file there and a path on another platform.
Backups leave those out rather than carry a name that does not travel.

## Implementing NetBBS Link

### Scope and reading order

Read [design sections 7–12](NetBBS-design-doc.md#7-netbbs-link-identity-events-and-compatibility)
for identity, events, synchronization, linked-resource lifecycle, mail,
files, and trust. The design reference specifies meaning; exact serializers,
validators, and fixtures pin the current wire format.

The implemented protocol supports linked message boards, channel messages,
file catalogues/on-demand chunks, ordinary home-node-encrypted mail, trust
signals, and real-time chat/presence. Board origin transfer and origin-authorized
post moderation exist. Do not infer channel/file-area origin succession,
delegated Link moderation, Link Communities, client-side mail decryption, or
cross-node `/dm` invites from a generic event envelope.

### Canonical bytes and signed identities

Durable event content uses an envelope containing `netbbs_protocol`,
`object_type`, and `payload`. The object type is part of the signed and
hashed bytes, providing domain separation. Wire dictionaries also carry
type-specific signature/content-ID fields: use each event's `to_dict`/
`from_dict` and builder/verifier pair rather than signing an arbitrary
serialization of the whole transport dictionary.

The binding canonicalization rules are:

- Normalize every string, including object keys, to Unicode NFC.
- Reject duplicate wire keys and keys colliding after normalization.
- Sort object keys by Unicode codepoint order at every nesting level.
- Use compact JSON separators and ASCII escaping (`ensure_ascii=True` in
  the reference implementation), then UTF-8 bytes. Non-ASCII characters are
  escaped, including surrogate pairs where required.
- Forbid floats. Integers must be within ±(2^53−1); booleans remain booleans.
- Distinguish absent fields from explicit JSON null. Follow each event's schema.
- Hash those bytes with the reference BLAKE2b content-ID parameters; do not
  substitute an identity fingerprint or a file-blob hash.

Reference: [canonicalizer](../src/netbbs/boards/content_id.py),
[event envelope and strict JSON parser](../src/netbbs/link/events.py), and
[canonical vectors](../tests/fixtures/link_canonical_vectors.json).
Reproducing the vectors is necessary for canonical-format compatibility,
but it does not alone prove protocol interoperability.

Node root keys authorize/revoke operational signing and transport keys.
Verify the transition chain and the currently authorized key before trusting
a message. User and node identities are distinct. Friendly names and managed
DNS names are presentation/routing claims; the fingerprint remains the
authority for peer identity, trust, and durable attribution.

Ordinary linked contributions use `node_vouched_user`: the home node vouches
for the author. The existence of other author-tier names in the design does
not mean their signing/decryption workflows are implemented.

### HTTP operations

All paths below have prefix `/link/v1`. The braces are path parameters.

| Method and path | Purpose |
| --- | --- |
| POST `/hello` | Mutual identity/endpoint exchange |
| POST `/events/{fingerprint}` | Push signed events |
| POST `/peers/{fingerprint}` | Authenticated peer discovery |
| POST `/inventory/{fingerprint}` | Signed bounded catch-up request |
| POST `/trust-pull/{fingerprint}` | Pull authorized trust material |
| POST `/file-chunk/{fingerprint}` | Request bounded file bytes and signed response metadata |
| POST `/relay-consent/{fingerprint}` | Negotiate relay consent |
| POST `/relay-mailbox/{fingerprint}/deposit` | Deposit relayed work for an unreachable recipient |
| POST `/relay-mailbox/pickup` | Authenticated pickup by the recipient |

The unauthenticated GET peers route exists only in compatibility/test contexts
with policy enforcement off; it is not the live-node discovery API.
HTTP success does not replace event verification.

Start with `HelloMessage`, `LinkNode.build_hello`/`handle_hello`, and
`LinkServer` in [protocol.py](../src/netbbs/link/protocol.py) and
[transport.py](../src/netbbs/link/transport.py). Follow the corresponding
`dial_*`, `request_*`, signing, verification, and `from_dict` functions
for each operation you implement. Do not assume all requests have the same
outer shape or authentication fields.

Inventory requests bind requester, intended responder, timestamp, and nonce
to the signature. Verified admission, clock-skew/replay checks, and trust
policy remain necessary even for an empty inventory. A hello exchanges identity
state; it does not transfer all resource history.

Implement bounded request sizes, response reads, resource counts, pending
events, retry storage, and replay caches. A single oversized event cannot be
allowed to block a queue indefinitely. Consult current source constants rather
than assuming a nominal event-count batch fits the transport byte limit.

### Event acceptance, storage, and catch-up

A receiver verifies format, signature, origin/author authority, trust policy,
and chain/dependency state before applying an event. A valid signature alone
does not authorize a moderation action or origin transfer. Unknown or incompatible
objects must follow the specified rejection/compatibility behavior, not become
unvalidated pass-through content.

Accepted events, local materialization, and search/unread projections are
different layers. Persist enough state to reconstruct them after restart.
A carried board must produce local readable posts, not merely retain envelopes.
Order chains by their dependencies, not solely by timestamps.

Inventory and pull transfer missed board/channel/file-area events through
carriers. A board's pre-promotion local history is not backfilled by promotion.
File-area synchronization carries metadata; bytes are fetched separately,
checked against signed descriptors and hashes, and may be unavailable after
the origin withdraws the file. A withdrawal is not a generic permission for
a peer to delete arbitrary local data.

Use [link storage](../src/netbbs/link/store.py),
[board bridge](../src/netbbs/link/boards.py), and
[synchronization](../src/netbbs/link/sync.py) alongside the protocol reference.
Inspect caller paths as well as builders: a defined primitive is not proof
that the live node exposes a workflow.

### Real-time transport

Real-time records use a separate versioned protocol over
`Noise_XX_25519_ChaChaPoly_BLAKE2s`. Authenticate the Noise static key against
the node's authorized transport identity and validate the encrypted version
advertisement before accepting application records. Bind an outbound session
to the fingerprint you intended to reach.

Length-prefixed ciphertext records are bounded to 65,535 bytes. Use the framing,
handshake, and record validators in [transport.py](../src/netbbs/link/transport.py)
and [protocol.py](../src/netbbs/link/protocol.py), including their timeout and
resource limits. The real-time listener is distinct from the HTTP endpoint;
do not send Noise bytes to an HTTP route.

Live relay proxies raw bytes below Noise. Endpoints still authenticate each
other; a relay does not acquire the endpoint's identity. One- and two-relay
paths are implemented. Live subscriptions carry chat/presence and bounded
scrollback-on-join, alongside asynchronous durable synchronization.
A session has one active chat surface; background simultaneous memberships
and cross-node invitation chats are not implemented.

### Interoperability evidence

Work in this order: canonical vectors; key/signature verification; mutual hello;
one linked resource and signed post; catch-up after restart/partition; malformed
or unauthorized traffic; then optional file, mail, trust, relay, and live features.

The [Link protocol tests](../tests/test_link_protocol.py),
[transport tests](../tests/test_link_transport.py), and
[sync tests](../tests/test_link_sync.py) provide executable examples.
The normative design and [readiness gate](NetBBS-phase4-readiness.md)
describe the broader adversarial cases. Record which features, versions,
platforms, and independent implementations you exercised. Passing Python
tests or canonical vectors alone is not a claim of public federation readiness.

## Two-node Link exercise

Use separate disposable directories, databases, identities, ports, and game
paths. Install the development dependencies above. This exercise is for
loopback; the SysOp handbook covers deployment.

Create `node-a/netbbs.toml` with:

```toml
[node]
identity_dir = "netbbs_identity"
name = "node-a"

[database]
path = "netbbs.db"

[ssh]
enabled = false

[telnet]
enabled = true
host = "127.0.0.1"
port = 2323

[link]
enabled = true
host = "127.0.0.1"
port = 7862
outgoing_only = false
advertised_host = "127.0.0.1"
advertised_port = 7862
seeds = ["http://127.0.0.1:7863"]
sync_interval_seconds = 5
```

For node B, use name `node-b`, Telnet port 2324, Link/advertised port 7863,
and seed `http://127.0.0.1:7862`. Configuring both seeds makes the intended
two-way exchange explicit without relying on discovery scheduling.

1. Run `python -m netbbs.admin --db node-a/netbbs.db` and repeat for node B.
   Create a SysOp in each and set distinct public **Node name** values through
   onboarding/Settings. TOML `[node] name` is the identity-file label, not that
   public name. Decline managed DNS and reliable-node participation for this
   isolated exercise; explicit `[link] enabled = true` still enables the local
   pair.
2. In two separate terminals using the environment's interpreter, change to
   `node-a` or `node-b` and run `python -m netbbs --config netbbs.toml`.
   The full-peer warning is expected for these loopback configurations.
3. Connect to each Telnet port and open **SysOp → Link status**. Confirm the
   peer identities. Record the fingerprints before testing restart.
4. On A, create a board through **Content → Message boards**, then use its
   **Link this board** action. Review and save the settings shown by the
   current editor. Post a new message after promotion.
5. On B, inspect/carry the linked resource as needed and browse the materialized
   post. Confirm author identity and body, not merely an incremented event count.
   Posts created before promotion remain local.
6. Send mail from A to the account on B, using B's full fingerprint as the node
   address to avoid naming ambiguity. Read the delivered mail on B and inspect
   the acknowledgement/delivery state on A.
7. Stop and restart A. Its fingerprint and peer state should persist. Repeat
   with B offline during a post, then verify catch-up after B returns.

The existing [quickstart smoke script](../scripts/link_quickstart_smoke_test.py)
covers this class of flow with real node processes and a scripted terminal
client. It still imports stdlib `telnetlib`, so run it with Python 3.11/3.12;
Python 3.13+ removed that module. The normal node is not dependent on it.
Use separate free ports and inspect its actual assertions before treating a
scripted run as evidence for a changed UI.

## Contributing to NetBBS

### Architecture and ownership

Read [AGENTS.md](../AGENTS.md), the
[design reference](NetBBS-design-doc.md), current issues, and the
[engineering record](NetBBS-worklog.md) before substantial changes.

| Package/surface | Responsibility |
| --- | --- |
| `auth`, `identity` | Accounts, authentication, key identities |
| `boards`, `files`, `chat`, `mail` | Synchronous local domain behavior |
| `communities`, `permissions`, `moderation` | Grouping, effective access, grants, audit |
| `storage` | SQLite, immutable migrations, database execution lanes |
| `net` | Sessions, terminal flows, transport orchestration |
| `rendering` | ANSI, display-column wrapping, screen/editor primitives |
| `link` | Federation protocol, persistence, distribution, local-domain bridges |
| `mrc` | Separate external chat bridge |
| `doors` | Registry, profiles, process/endpoints, services, bundled games |
| `web/` at repository root | Public project website |
| `src/netbbs/web/` | Browser caller transport and packaged assets |
| `services/` | Separately deployed project services, not automatically installed with the BBS |

Domain functions remain synchronous and `db`-first. Async flows dispatch database
work through the owning `DatabaseLane`; do not run blocking SQLite work on
the event loop or share a connection across arbitrary threads. Keep local domains
independent of Link: bridge modules may call local domains, not the reverse.

Use transactions for multi-row invariants and real connections for contention
tests. Shipped migrations are immutable. Test table rebuilds with realistic
foreign-key children; a successful empty-database migration is insufficient.

The creator of an async task owns cancellation, gathering, and failure retrieval
on every exit path. Cleanup failures must not mask the original exception.
Bound resources influenced by callers/peers and make exhaustion visible.

### Terminal and interaction contracts

Ordinary output uses `Session.write_line` and prompts use `write_prompt`.
Wrap by display columns, accounting for wide/combining characters, tabs, and
cursor controls. Sanitize untrusted segments before styling; never sanitize
a completed trusted ANSI composition. Fixed artwork uses preformatted output
but must still fit or wrap.

Screens reached by a hotkey show content first and provide a way back without
writing. Collect multi-field edits in a draft; save once. A rejecting save must
raise the editor's designated error so the draft stays open. See design
[section 3.5](NetBBS-design-doc.md#35-interaction-model-for-screens-issue-282)
for confirmation and draft behavior.

For bundled door presentation, inspect real renders, not just width tests.
[door_gallery.py](../scripts/door_gallery.py) renders Voidrunner and War Dialer;
add appropriate walks/fixtures for changed screens and attach pictures to the
PR. Retro Trivia requires a direct run. Respect each game's normative presentation
contract in the design reference.

### Documentation changes

Handbooks describe current behavior for their audience. Put durable product
or protocol decisions in the design reference, engineering constraints in the
worklog, and implementation history in commits/PRs. Do not copy entire reference
sections into multiple audience guides or promote historical release claims
into current guarantees.

The [website source and maintenance guide](../web/README.md) distinguishes
repository edits from deployment. Preserve real capture bytes and LF endings.

## Node configuration reference

The startup parser lives in [net/nodeconfig.py](../src/netbbs/net/nodeconfig.py).
This is separate from [config.py](../src/netbbs/config.py), which stores
database-backed settings. CLI overrides TOML; live settings are not all TOML
options. A TOML file requires explicit `--config`; it is not auto-discovered.
Use `python -m netbbs --help` for the complete installed CLI.

| TOML table | Key settings and defaults |
| --- | --- |
| `[database]` | `path = "netbbs.db"` |
| `[node]` | `identity_dir = "netbbs_identity"`; `name` labels generated key identities, not the public display name |
| `[ssh]` | Enabled on `0.0.0.0:2222` |
| `[telnet]` | Disabled; default bind `127.0.0.1:2323` |
| `[web]` | Disabled; default bind `127.0.0.1:8080`; `public_url` sets the external transfer-link base |
| `[link]` | Explicit `enabled` overrides stored participation; `outgoing_only = true`, HTTP port 7862, real-time port defaults to HTTP port + 1000 |
| `[throttle]` | Cross-connection login-attempt controls |
| `[shutdown]` | Caller warning delay and bounded background-task drain |
| `[managed_dns]` | `service_url` points the node at a different managed netbbs.org service (https-only away from loopback; a node already registered elsewhere pauses rather than carrying its credential across); unset means the shipped address in `netbbs.managed_dns.state.DEFAULT_SERVICE_URL`. `admin_token` is the service's `MANAGED_DNS_ADMIN_TOKEN`, for the one node whose operator runs the service; config file only, and its presence puts the service-administration screen on that node's SysOp console |

Link configuration additionally includes seeds, sync interval, advertised
addresses, relay consent/capacity, real-time listener settings, peer/carried-resource
caps, request rate/burst, and trust transport limits. The field definitions,
TOML loader, and validation in `LinkConfig`/`NodeConfig` are the complete
reference for accepted names and ranges. Unknown invented fields do not enable
a missing feature. Read [nodeconfig tests](../tests/test_nodeconfig.py) for
precedence, invalid values, and compatibility cases.

Use distinct state paths for concurrent test nodes. Relative paths are relative
to the process working directory, not automatically to the TOML file's directory.
Bundled-game environment overrides need the same isolation; otherwise two test
nodes under one OS account can unintentionally share a default save directory.

## Validation and release work

Run focused tests that exercise the changed behavior through real boundaries.
For example, from the active development environment:

```sh
python -m pytest tests/test_link_canonical_vectors.py tests/test_link_protocol.py
python -m pytest tests/test_doors_runtime.py tests/test_doors_resize.py tests/test_doors_outbound.py
```

Choose the relevant command, rather than running both for unrelated changes.
For the full suite, use `python -m pytest`. On Windows, a writable test temp
directory and `-p no:cacheprovider --basetemp=<path>` can avoid sandbox/temp
permission problems. Use a disposable basetemp directory; pytest manages it.

For doors, inspect [runtime tests](../tests/test_doors_runtime.py),
[resize tests](../tests/test_doors_resize.py),
[service tests](../tests/test_doors_services.py), and
[outbound tests](../tests/test_doors_outbound.py). POSIX-specific coverage must
run on an appropriate host; Windows skips are not passing POSIX evidence.
The [door guide's matrix](NetBBS-door-guide.md#what-is-supported-and-what-has-been-verified)
records the bounded host/game profiles.

For release work, build with `python -m build`, install the wheel into an
isolated environment outside the checkout, and verify version, packaged assets,
bootstrap, and a real caller path. Preserve required extras. Do not treat an
editable install as evidence that wheel assets are present.

Use repository-local checks and review; hosted CI is not a requirement for
this project's workflow. Record actual test results and remaining manual
checks in the PR/issue. Do not describe an infrastructure waiver as a pass.
Public federation, balance, human usability, target-host compatibility, and
disaster recovery each need the corresponding operational evidence.

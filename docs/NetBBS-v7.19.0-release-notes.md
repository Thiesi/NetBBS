# NetBBS v7.19.0

This release covers everything merged since v7.18.6. It has three parts.

- **Chat bots** (design #1208, implementation #1210). A SysOp can run Baud,
  the bundled house bot, or an external bot program in chat channels. Every
  bot is off until a SysOp configures it, and every ability beyond answering
  messages addressed to it is a separate grant: reading whole channels,
  greetings, statistics and last-seen queries, board and file
  announcements, Linked channels, and an optional language model.
- **The bundled games come from their own repositories** (#1214, #1216,
  #1223, #1226, #1227, #1228). Voidrunner, Retro Trivia and War Dialer are
  now developed in Thiesi/Voidrunner, Thiesi/RetroTrivia and
  Thiesi/WarDialer, which can also host them on other BBS software. NetBBS
  ships one tagged release of each, unchanged: Voidrunner v1.1.0, Retro
  Trivia v1.0.0 and War Dialer v1.1.0.
- **Chat fixes.** The chat is drawn again after a resize (#1207), and
  command hints colour their brackets and parameter names differently
  (#1233).

**It migrates: the node database goes from schema 123 to 130.** Other
versions stay as they were: `NETBBS_PROTOCOL_VERSION` is 1,
`REALTIME_PROTOCOL_VERSION` is 4, `DOOR_API_VERSION` is 4, Voidrunner careers
are save schema 2 and War Dialer worlds are world schema 11. `netbbs.toml` has
no new keys. There is a new optional extra, `netbbs[bots]`, needed only for
model conversation. Rolling back needs a restore, and Voidrunner careers saved
by this release cannot be read by 7.18.6 (see *Upgrade and rollback*).

## Chat bots (#1208, #1210)

### What callers and SysOps gain

- **Baud, under SysOp → Content → Open chat bots.** Create an instance,
  choose its channels, optionally make it their default bot, enable it and
  Save. Callers type `Baud.bot: help`, or `!help` where it is the default.
  It sets public reminders (`!remind 10m check the oven`, `!reminders`,
  `!cancel 1`) that survive restarts. Friendly, terse and retro temperaments
  change its phrasing (#1212).
- **External bots** use the same versioned JSON-lines API and the same host
  checks as Baud, documented in the new [bot API guide](NetBBS-bot-api.md).
  They run as the NetBBS OS user; the API bounds what they can do through
  NetBBS, but it is not an operating-system sandbox. Install only programs
  you trust.
- **Bots are marked and controllable** (#1211, #1212). A bot author is a
  reserved `.bot` name shown with `[BOT]` in live chat, history and rosters.
  `/bots all|local|none` sets which bots a channel shows on this node.
  Moderators can `/mute Baud.bot`. The catalogue shows each bot's status,
  with **Kill selected**, **Stop all** and **Delete selected** (#1215,
  #1232). Output limits apply per bot and channel and survive a restart.
- **Separate grants, all off by default:**
  - **Linked channels** (#1213). With a per-channel **Linked approvals**
    entry, callers on other nodes address `Baud.bot@Node: help`. New Link
    frames are offered only to peers that announce the `chat_bots_v1`
    capability; older peers keep receiving ordinary chat.
  - **Statistics and last seen** (#1218): `stats` gives the node name,
    uptime and how many people are online; `seen <account>` gives public
    online status or the last recorded login or logout. Hidden session
    history stays hidden.
  - **Reading whole channels, joins and leaves, and greetings** (#1219). The
    editor asks before Save, and the channel heading and `/who` tell callers
    which bots read the channel.
  - **Post and file announcements** (#1220) from boards and file areas that
    everyone can read, into one announcement channel per bot. They carry
    titles and names, never bodies or files, and they are best-effort
    notices, not a guaranteed delivery queue.
  - **Model conversation** (#1222). A node-wide provider switch under
    **Chat bots → Model settings** and a per-bot grant, both off. Any
    OpenAI-compatible endpoint works, for example a local Ollama server;
    NetBBS installs no model. A caller's first request only shows a notice
    naming the provider and is not sent. Replies are public, and context is
    limited to that caller's recent exchange with the bot in that channel.
    Token budgets, optional money caps, per-caller quotas and channel rates
    bound it, and **Usage** shows what was spent. Prompt logging is off by
    default. API keys stay in the node database and are never passed to a
    bot program.
- **Bot state is backed up** (#1215). Reminders and other bot state in
  `<db>.bots/` are part of node backups and restore with the database.

## The games' own repositories (#1214, #1223)

The three games keep running exactly as before inside NetBBS, from the same
Gallery entries. What changes is where they are developed: each repository has
its own tests, design notes and releases. NetBBS bundles a tagged release
unchanged, recorded in `src/netbbs/doors/bundled/VENDORED.json`, and its tests
refuse a modified copy. **Report game bugs in the game's repository**; world,
save, backup and resize behaviour inside NetBBS is still reported to NetBBS.

What the new releases change for NetBBS callers:

- **Voidrunner v1.1.0** shows how long is left before the door's time limit
  ends the session, warns before it runs out, and saves before handing the
  caller back. The limit is the door profile's `time_limit`, or an hour for
  a door without a profile; a limit of 0 means none, and nothing is shown. Its Display
  Options screen names the encoding as managed by NetBBS. Every save now
  records a display encoding, which matters on rollback (below).
- **War Dialer v1.1.0** plays its launch splash on Windows nodes too. It
  already played on other systems, and Windows now detects input waiting
  in a pipe.
- **Retro Trivia v1.0.0** no longer fails to start on a Windows node running
  Python 3.11 (RetroTrivia#7), where the copy in 7.18.6 asked for a call
  that Python version lacks on Windows. Otherwise it plays the same inside
  NetBBS; its new features (DOOR.SYS and DOOR32.SYS, CP437, DOOR32 sockets)
  are for other BBS software.

War Dialer's season and reset controls now live in the door, behind its own
`--admin` command (#1227). The SysOp console's **[W]orld** screen and
`python -m netbbs.doors.war_dialer_admin` work as before: NetBBS still checks
that the world belongs to the node and still takes and verifies a complete
node backup before a season close or reset.

## Chat fixes

- **Resizing a chat window keeps the conversation** (#1207). Making the
  window taller or shorter redrew only the status and input rows and left
  the history blank. The chat now redraws its most recent lines (up to 500
  are kept) at the new size, in both channel and MRC chat and in direct chat.
- **Command hints are easier to read** (#1233). In `Usage:` lines and
  `/help`, brackets and `|` are grey and parameter names light blue.

## Upgrade and rollback

Take a backup, stop NetBBS, replace the wheel and start it. Seven additive
migrations run on the node database, taking it from schema 123 to 130. All
are part of #1210:

- **124:** a per-channel bot display policy (`channels.bot_policy`, default
  `all`) and the author kind of stored chat lines
  (`channel_messages.author_kind`, default `user`).
- **125:** bot instances, their channel grants and persistent output
  history.
- **126:** per-channel Linked approval (`bot_channels.linked_approved`,
  default off) and mutes of remote bots.
- **127:** statistics and last-seen query grants (`chat_bots.queries_json`,
  default none).
- **128:** channel reading, presence events and greetings, all off.
- **129:** the announcement channel and its post and file toggles (off),
  board and file-area event sources, and the bounded event journal.
- **130:** the model conversation grant (off), model usage accounting and
  limits.

`netbbs.toml` has no new keys; bot and model settings are stored in the node
database and set from the console. For model conversation only, install the
`bots` extra (`pip install "netbbs[bots]==7.19.0"`), or have `web` installed,
which already brings its HTTP client. The systemd and NetBSD rc.d examples did
not change.

On the first start after the upgrade:

- **No bot runs** until you create, configure and enable one, and nothing
  reaches a model until you enable a provider *and* grant it to a bot.
- **Every channel shows all bots** (`/bots all`), which is harmless until a
  bot exists. Set `local` or `none` per channel to show fewer.
- **A local account whose name already ends in `.bot`** stays a person. New
  accounts cannot take such names.
- **MANUAL — before granting model conversation,** read the bot API guide's
  *Optional model conversation*. Set the token budget, caller quotas and
  any money caps first, and use HTTPS for a remote provider.

**Rolling back needs a restore.** A 7.18.6 wheel refuses a schema-130
database. **MANUAL — to roll back:** stop NetBBS, install the 7.18.6 wheel,
then restore the backup taken before the upgrade. Anything since the upgrade
is lost with it. In particular:

- **Voidrunner careers played since the upgrade** are refused by 7.18.6's
  Voidrunner ("The saved career contains unsupported fields"), because
  v1.1.0 adds a display encoding to every save. Restoring the pre-upgrade
  backup brings back the careers as they were then, so progress made since
  is lost. War Dialer worlds are unaffected: 7.18.6's War Dialer ignores the
  new preference.
- **MANUAL — delete the bot state directory, `<db>.bots/`,** after the
  restore. 7.18.6's restore predates bots and leaves it in place, and a bot
  created under the same name after a later upgrade would pick up its old
  reminders. The bots themselves go with the restored database.

## Verification boundaries

- **The release gate:** the full suite (`pytest -n auto`) on the tree that
  was merged as #1230, #1231 and #1232: 11,726 passed, 136 skipped, 1
  failed under load. The failure
  (`test_bot_scopes::test_real_chat_heading_who_actions_join_leave_and_no_replay`)
  passed three times on its own. The `timing_sensitive` test passed 1/1.
  #1233 was covered by the chat and console-convention suites (794 passed)
  on top of that tree, not by a second full suite. The suite is about 3,000
  tests smaller than at v7.18.6 because the games' own tests moved to their
  repositories, where each runs on Linux and Windows with Python 3.11 and
  3.13. NetBBS itself still has no test CI; its suite ran on Windows only.
- **Chat bots** are tested with real Baud and external-program subprocesses,
  real SQLite, multi-node Link sessions and a fake local HTTP model
  provider. No real model server (Ollama, llama.cpp or a hosted API) was
  used. The bots have not been walked through on NetBSD, or in SyncTERM or
  another real terminal.
- **Mixed-version Link** for bots is tested against simulated older peers
  that lack the new capabilities, not against a running 7.18.6 node.
- **The bundled games** were checked inside NetBBS by its host tests:
  launch, resize, guest calls, saves, backup and War Dialer administration.
  Their classic-host features (drop files, CP437, DOOR32 sockets) are tested
  upstream only, and live Synchronet field tests are still open
  (Voidrunner#6, RetroTrivia#5, WarDialer#6).

# NetBBS v7.43.0

Lunar Lander grows up, and its pictures work in more terminals:
- **Lunar Lander v0.5.1:** 50 achievements, a Pilot Record screen and
  Programs, monthly seasons that every pilot on the node flies.
- **Quarter blocks per caller:** PuTTY's default fonts show the quarter
  blocks Lunar Lander draws with as boxes. NetBBS now notices PuTTY and
  sends half blocks instead, and every caller can set it either way.
- **Season announcements:** a game can announce the start of a new season
  in chat.

This release keeps the feature freeze declared in v7.40.0. The new Profile
setting, the `door_info.json` field and the new event type are
maintainer-approved exceptions (#612).

**This release migrates:** the node database goes from schema 163 to 164.
Rolling back to v7.42.0 needs a restore from backup (see below).

**Versions:** the Link real-time protocol stays at 4, the door API at 5 and the
bot API at 1. The door API gains one optional field and one event type
without a version change.

## Lunar Lander v0.5.1 (PR #1566)

The bundled Lunar Lander moves from v0.1.0 to v0.5.1, from its own
repository, [Thiesi/LunarLander](https://github.com/Thiesi/LunarLander).

- **Achievements:** 50 of them, and a Pilot Record screen, <kbd>P</kbd>, that
  shows each pilot's.
- **Programs:** a monthly season shared by every pilot on the node. Fly the
  program's missions in the simulator, then make one real flight each. The
  Cadet, Astronaut and Test Pilot presets set how hard it is.
- **Launch scene:** a three-second scene before each flight.
- **Game events:** with the door's game events switched on, silver and gold
  achievements and completed programs are announced. So is each new season
  (see below).

- **Fonts without quarter blocks:** for a caller with Quarter blocks off (see
  below), the game draws its scenery, sprites and wordmark in half blocks
  itself, the way it does for CP437.

**Records change format.** From v0.5.0 the game keeps achievements and programs in the same
`pilots.db`, so its records schema goes from 1 to 2. The game upgrades the
file on its first launch by only adding tables, so every pilot's records carry
over.

## Backups know the new records (PR #1564, issue #1561)

A node backup checks Lunar Lander's records before taking them. It now
accepts schema 1 and schema 2, each with its exact set of tables, and still
refuses anything else. A backup taken under v0.1.0 restores under v0.5.1,
which upgrades the file again on its next launch.

**Running Lunar Lander v0.5 by hand on an older NetBBS?** Its backups refuse
the new records until you upgrade NetBBS. This release is the one that
accepts them.

## Quarter blocks per caller (PR #1563, issue #1560)

Lunar Lander draws its scenery with quarter blocks (`▗▟▙▖`). A UTF-8 terminal
whose font lacks them shows boxes instead, and PuTTY's default Windows fonts,
Courier New and Consolas, are like that.

**Profile ▸ Quarter blocks** sets what each caller gets:

| Setting | Means |
| --- | --- |
| **Auto** | The default. Off for PuTTY, on for every other terminal. |
| **On** | Quarter blocks, for a font that has them. |
| **Off** | Half blocks in their place: a coarser picture, but every font has them. |

The setting shows a sample of quarter blocks, so a caller can see at once
whether their font has them.

- **How Auto finds PuTTY:** NetBBS asks each Telnet and SSH terminal for its
  answerback. PuTTY answers `PuTTY` unless its user changed that.
  - **Telnet:** the question rides the existing terminal-type exchange, so
    logins are no slower.
  - **SSH:** the wait ends after one round trip. A terminal that answers
    nothing holds the first screen up for at most half a second.
  - **Browser:** the web terminal is never asked.
- **The node log** says when it happened: the connection's detection line
  ends `answerback 'PuTTY', no quarter blocks`.
- **Doors:** `door_info.json` gains `quadrants`, true when the caller's
  terminal is sent quarter blocks. When it is false, NetBBS replaces each
  quarter block in a door's output with the nearest half or full block, so no
  door shows boxes.

**Profile field numbers move up by one** after Character set: for example,
SSH keys is now 26 and Password 27. Callers who jump to a field by its number
will notice.

## Season announcements (PR #1565, issue #1561)

Game events gain a sixth type, `season`, which names no caller: a game's own
news. With the door's game events on and a bot granted the door, it reads
like this:

```
A new Lunar Lander season begins: 7 missions, through November.
```

It counts against the door's hourly event ceiling like any other event, and
it is announced only while every caller may play the door. The Developer
Handbook describes it.

## Upgrade and rollback

- **Upgrade:** take a backup, install the wheel and restart. The database
  migrates to schema 164 on start. Lunar Lander upgrades its records the first
  time a caller launches it.
- **MANUAL: rollback to v7.42.0 needs a restore.** v7.42.0 refuses a
  schema-164 database, so reinstalling the old wheel alone won't start the
  node. Restore the backup you took before upgrading, using v7.42.0. Its
  Lunar Lander records are then schema 1 again, which v0.1.0 reads.
- **MANUAL: Lunar Lander records after a rollback.** If callers flew v0.5.1
  before you rolled back, the live records are schema 2, which v0.1.0
  refuses. The restore replaces them with the backup's copy. Achievements and
  programs earned since the upgrade are lost.

## Verification boundaries

- **Quarter-block detection has not been tried against a real PuTTY.** The
  answerback is PuTTY's documented default, and the probe is tested against
  a simulated client over a real socket.
- **Unknown answerbacks reach the caller's input.** A terminal set up to
  answer ENQ with something else delivers that answer to the first prompt as
  if typed. No common terminal does this by default.
- **SSH probe on slow links:** a terminal that answers neither question holds
  the first SSH screen up for at most 0.5 s.

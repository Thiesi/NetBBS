# NetBBS v7.18.0

This release covers everything merged since v7.17.1. Most of it is one
change, the hotkey overhaul (tracker #1158): the same keys now do the same
thing on every screen, and a menu key is always the first letter of its
item. That moves keys callers have learned, so read *Keys that moved* before
upgrading. It has three parts.

- **Keys that work everywhere** (#1159–#1164, #1170, #1175). `<` `>` page,
  `/` finds, `?`, F1 and Ctrl-H open help, and `B` or Esc goes back, on every
  hotkey screen. Every menu key is its label's first letter, list rows are
  numbered `01`, `02` …, and settings fields are chosen by number. Each
  existing account sees a one-time screen explaining the change.
- **Styled MRC names** (#1156, PR #1157). Each caller chooses how an MRC
  sender's decorated handle, such as `+Nick+[CASTLE BBS]`, is shown.
- **Node pages on www.netbbs.org** (tracker #1165: #1166–#1169, #1174). A
  node with a managed netbbs.org name that Reliable Link has met can have a
  public page at `https://www.netbbs.org/~<name>`, with a member badge for
  its own website. The node signs its choice (shown, indexed or off) into
  its Link descriptor. **The pages are not live yet**: the release ships the
  setting, the export and the generator; the deploy on the project's server
  follows this release (see "Verification boundaries").

**It migrates: the node database goes from schema 120 to 123.** Other
versions stay as they were: `NETBBS_PROTOCOL_VERSION` is 1,
`REALTIME_PROTOCOL_VERSION` is 4, `DOOR_API_VERSION` is 4, Voidrunner careers
are save schema 2 with tactical and outclassed ruleset 3, and War Dialer worlds
are world schema 11. `netbbs.toml` has no new keys. The Link wire gains three
optional fields in the signed endpoint descriptor, which older nodes accept
and ignore: `node_page`, `software_version` and `public_boards`; see
*Upgrade and rollback*. Rolling back needs a restore.

**Keys that moved.** Every rename is a clean switch: the old key is gone
with no hidden alias, and on most screens it now does something else or
rings the bell.

- **Paging is `<` `>`** (also PgUp/PgDn, and ←→ on lists) everywhere. The
  letters that used to page are gone: `[N]ext`/`[P]rev` on pickers, the
  mailbox and the post/letter review screen, `[O]lder`/`[N]ewer` on the post
  and file lists, and `n`/`p` on detail panels. The board reader's `[N]ext post` and `[P]revious post` stay.
- **Find is `[/]`** in the mailbox too (`[F]ind` is gone).
- **Main menu:** Communities `C[o]mmunities` → **`[T]opics`**; the staff list
  `S[t]aff list` → **`[O]perators`**; `P[r]evious callers` →
  **`[R]ecent callers`**; `Moder[a]tion` → **`[A]pprovals`**. `O` and `T`
  swapped places; `R` and `A` kept their letters.
- **Mail list:** `De[l]ete` → **`[E]rase`**; `K[e]ep` / `Mov[e] to Inbox` →
  **`[K]ept: no/yes`**; the Kept folder `[K]ept` → **`[V]iew Kept`**;
  `Delete [r]ead` → **`[P]urge read`**; `[U] Read` → **`[U]nread`**. Note
  that `E`, `K` and `P` now mean something different on this screen: `E`
  used to keep a letter and now erases it (after a yes/no question that
  defaults to No), `K` used to open the Kept folder and now keeps a letter,
  and `P` used to page back and now purges read mail (after a question that
  defaults to No).
- **A letter:** `Reply [a]ll` → **`[A]nswer all`**; `[D]elete` → **`[E]rase`**;
  `K[e]ep` / `Mov[e] to Inbox` → **`[K]ept: no/yes`**; `[U]nread` →
  **`[U]nread: no`**; `Bloc[k] sender` → **`[S]ender blocked: no/yes`**. Here
  too `E` used to keep and now erases (default No), and `K` used to block the
  sender and now keeps.
- **A sent letter:** `Re[s]end` → **`[S]end again`** (or **`[S]end another
  copy`**); `[D]elete` → **`[E]rase`**.
- **Post reader:** `Remove pos[t]` → **`[T]ake down`**; `P[i]n` →
  **`[O]n top: no/yes`**; `Un[k]eep` → **`[K]ept: no/yes`**.
- **Board and file area:** `Un[f]ollow` → **`[F]ollow: off/on`**; a file's
  `P[i]n` → **`[O]n top`**.
- **Who's online:** `Bloc[k]` → **`[S]ender blocked: no/yes`**.
- **Sort prompts:** `A[L]phabetical` → **`[N]ame`**.
- **Writing a post or letter, the review screen:** `[B]ody` →
  **`[E]dit body`**; `[C]ancel` → **`[B]ack`**, which now asks "Discard this
  draft?" (default No) once a body is written.
- **Row numbers:** on a board's posts, the mailbox and a file area, a row is
  `01`, `02` …: type two digits, or one digit and Enter. A single digit no
  longer opens a row by itself.
- **Settings** (Profile, Name & details, the SysOp console's settings and
  editors, a resource's own screen): fields are chosen by number,
  `01`–`99`, numbered straight through a screen's sections, instead of by
  letter. The console's per-user account screen keeps its letters.
- **SysOp console:** every key now sits on its label's first letter, many
  with a reworded label. Keys that moved include `A[w]ay` → **`[T]ime away`**,
  `Bac[k]up` → **`[F]ull backups`**, `[O]utbox` → **`[Q]ueue (outbox)`**,
  `Node lo[g]` → **`[E]rror log`**, `[F]ollow log` → **`[W]atch log`**,
  `Limit[s] & retention` → **`[L]imits & retention`**, `Net[w]ork & login
  limits` → **`[O]perating limits`**, `Pr[o]motion rules` →
  **`[A]uto-promotion rules`**, `Re[t]ired names` → **`[H]eld names`**,
  `Moderate [E]verything` → **`[G]lobal moderation`**, and the trust state
  `[B]locked` → **`[D]enied`** (its quick action `Bloc[k]` → **`[D]eny`**). Settings no longer has its hidden `l o r d f
  k` shortcuts into Operations screens; each of those screens keeps its key
  under Operations. Granting a moderator every board, area or channel now
  takes `[A]ll of one kind`, then the kind. The SysOp Handbook has the full
  set.
- **New:** the Managed DNS screen gains **`[W]eb page`**; Profile gains the
  **Stylized MRC names** setting (see below).

## Keys that work everywhere (#1158)

The decisions are in design doc §3.5 "Keys that work everywhere" and §16
"Issue #1158" (Decisions 1–8), each with the alternative it rejected. The
User Handbook has a table of the keys in "Find your way around".

### What callers and SysOps gain

- **The same keys on every hotkey screen**, on Telnet, SSH and the browser
  terminal alike (PRs #1160, #1161):
  - `B` or **Esc** is Back. Esc first drops a highlighted row, then goes
    back. On a form it takes the `[B]ack` path, with its "Discard unsaved
    changes?" question. Before, Esc did nothing on most screens, and on lists
    and forms it only dropped a highlight or rang the bell.
  - `<` and `>` page; so do PgUp/PgDn, and ←→ on lists and long text. On a
    form, ←→ still step the highlighted value, and `<` `>` switch sections.
  - `/` finds.
  - Enter chooses.
  - `B` is the only reserved letter. Line prompts (typing a name, a
    subject) and doors are outside the rule.
- **Help on every hotkey screen** (PR #1162). `?`, F1 and Ctrl-H open help.
  Before, `?` worked only on the main menu, and most console menus, the file
  area list, detail panels and a dozen caller screens rang the bell. Help is
  built from the screen's own menu: each key with its one-line description, a
  sentence on what the screen is for, and the keys that work everywhere. A
  `?` typed into a post or letter is still a `?`. The browser terminal now
  decodes F1, and a click on `[<]` or `[>]` is sent instead of dropped.
- **Every menu key is its label's first letter** (PR #1164): no key in the
  middle of a word, on a later word, or set apart from its label. Toggles show
  their state (`[F]ollow: on`), so the key never changes with the state. The
  mail list's `[M]ark` and `[U]nread` are the one exception: the row already
  shows `*` and `new`.
- **Two-digit row numbers** (PR #1170). A board's posts, the mailbox and a
  file area took one digit, `1`–`9`, so rows past the ninth (the mailbox
  shows up to 30) had no number. They now work like the pickers: `05`, or
  `5` and Enter. A number beyond the page, `00`, or a second key that is not
  a digit rings the bell and clears what was echoed.
- **Numbered settings** (PR #1175). Profile runs `01`–`24`, for example.
  Typing a number from another section turns to that section and opens the
  field. The action bar shows `[01-NN] change` instead of a row of field
  letters, so forms are shorter; with menu descriptions on, the highlighted
  field's description appears under the list. ↑↓ with Enter still work. The
  handbooks now name settings by label ("Profile → Character set").
- **A one-time notice** (PR #1161, extended by #1164, #1170, #1175). Every
  account that exists at the upgrade sees "Keys that work everywhere" once,
  at its next caller login (Telnet, SSH or browser; not the local SysOp
  console), after the Recent callers screen. It lists the shared keys, the
  Topics/Operators and mail renames, two-digit rows and numbered settings.
  Accounts made after the upgrade never see it, and guests never do.
- **Menu art that draws old keys** (PR #1164; main-menu art only). A SysOp's main-menu art that
  still draws `C[o]mmunities` or `S[t]aff list` would send a caller to the
  wrong screen, since `O` and `T` swapped. Such a drawn item is now blanked
  and the real one listed in `{menu}` until you redraw it. Art drawing
  `P[r]evious callers` and `Moder[a]tion` keeps working (same letters) but
  reads better redrawn. **Check** and **Preview** on the art screen show what
  is blanked and moved.
- **For contributors:** `tests/test_reserved_keys_enforced.py` and
  `tests/test_first_letter_keys.py` read the source and fail on a screen that
  breaks the rules (doors excluded). `FieldSpec` no longer takes a letter,
  and `DetailAction` refuses a reserved key or a digit when built. The
  Developer Handbook's "Terminal and interaction contracts" explains the
  rules.

## Styled MRC names (#1156, PR #1157)

Since v7.17.1 an MRC sender's decorated handle is peeled off the message text
(#1152). A new Profile setting, **Stylized MRC names** (Communication
section), chooses how it is shown:

| Setting | Shown |
| --- | --- |
| **combined** (default) | `<+Michael_Nln+@Castle_of_the_Gods_V (CASTLE BBS)> hello all` |
| **both** (the v7.17.0 look) | `<Michael Nln@Castle_of_the_Gods_V> +Michael_Nln+[CASTLE BBS] hello all` |
| **label only** (the v7.17.1 look) | `<Michael Nln@Castle_of_the_Gods_V> hello all` |

- The choice is applied per viewer when a line is drawn, so it covers
  scrollback too: earlier lines show in the new style the next time the room
  is drawn. MRC private and broadcast lines follow it too.
- The BBS name always comes from the packet header, never from the styled
  name. The tag in brackets has `()[]{}<>@|~*=` and pipe colour codes
  removed and is cut at 24 columns.
- The default, **combined**, changes how decorated lines look compared with
  v7.17.1. Lines received before the upgrade carry no stored handle and look
  the same in all three styles.

## Node pages on www.netbbs.org (#1165)

The rules are in design doc §8.13 "Public node pages" and §16 "Issue #1165"
and "Issue #1171"; the SysOp Handbook describes the setting.

### What a SysOp gets

- **A page at `https://www.netbbs.org/~<name>`** for a node that holds a
  managed netbbs.org name (matured, or since abandoned or released) and that
  Reliable Link has met directly. It shows the node's friendly name, its
  dial-in addresses, its technical identity (fingerprint), when the name was
  registered, how long Reliable Link has known it, when it was last heard,
  and a state: **active** (heard within 7
  days), **quiet** (within 30), or **left** (longer, never, or name
  released). Since #1174 it also shows the NetBBS release as major.minor
  ("NetBBS 7.18", never the patch level) and the Linked boards the node's
  guest account may read.
- **Little more than a caller on Reliable Link sees.** The page is built
  from ReLink's node map as a caller sees it, plus the registration date from
  the managed-DNS service, through a fixed list of fields: never Link network
  addresses, relays, reliability, trust states or the address the DNS service
  sees. A pending name has no page; a revoked name loses
  it. History is keyed by node fingerprint, so a name that passes to another
  node starts a fresh page.
- **Public boards are listed only if a guest could read them.** A board is
  listed when the node's designated guest account passes its read and age
  gates. Guest login off, a disabled guest, a SysOp-level guest account, or
  a guest account awaiting approval or holding a privilege lists none. Hidden, closed and local boards are never listed; at most 24,
  by Link name.
- **The setting: `[W]eb page`** on the SysOp console's Managed DNS screen,
  shown while the name is pending or matured. It steps through **shown, not
  indexed** (the default), **shown and indexed**, and **off**, and the change
  is audited (`set_node_page`). The node signs the choice into its endpoint
  descriptor, so it reaches the page at the node's next contact with Reliable
  Link. With **off**, the descriptor carries neither the release nor the
  board list.
- **A member badge**, `https://www.netbbs.org/~<name>/badge.svg`: "NetBBS
  Link, member since ⟨month year⟩ · ⟨state⟩". It holds no text the node
  supplied. The node page offers an HTML snippet to paste into the board's
  own website.
- **`python -m netbbs.admin export-node-map [--output FILE]
  [--identity-dir DIR]`** prints the node map as this node's callers see it,
  as JSON. It needs no passphrase and is safe while the node runs. ReLink's
  export is what the pages are built from; on any other node it is for
  looking.
- **The generator** is `services/node_pages` in the repository (the tag or
  GitHub's **Source code** archive; it is not in the wheel or the
  `netbbs-7.18.0.tar.gz` sdist), standard library only. It is project infrastructure, not part of a
  node.

### How older nodes take it

- **Older nodes accept the new descriptor fields.** A v7.17.1 node checks
  the descriptor's signature over the whole signed envelope, its subject and
  its name fields; it has no list of allowed fields, so `node_page`,
  `software_version` and `public_boards` are stored and passed on unread.
- **An older node is published by default.** It cannot send `node_page`, so
  its descriptor reads as "shown, not indexed", and its page shows no release
  and no boards. **A SysOp who wants no page must upgrade and set
  `[W]eb page` to off.**
- **New readers never fail a hello.** A malformed `node_page` reads as off
  ("a claim not understood is not consent to publish"); a malformed release
  or board entry is dropped.

## Upgrade and rollback

Take a backup, stop NetBBS, replace the wheel and start it. Three migrations
run on the node database, taking it from schema 120 to 123:

- **121: `channel_messages.mrc_handle`** (PR #1157), the decorated handle as
  sent. NULL for every existing line.
- **122: the keys notice** (PR #1161). Every existing account, guest account
  included, gets the preference `keys_notice_1158 = pending`; the notice
  itself skips guests.
- **123: `link_peers.first_contact_at`** (PR #1167). Existing rows are
  backfilled with the earliest of `updated_at`, `last_direct_contact_at` and
  `descriptor_first_stored_at`.

`netbbs.toml` has no new keys. The node-page choice is stored in the node
database (config key `link_node_page`) and set from the console. The systemd
and NetBSD rc.d examples did not change.

On the first start after the upgrade:

- **Every caller with an account meets the new keys**, and sees the one-time
  notice at their next login. **MANUAL —** tell your callers, for example in
  a bulletin, that `O` and `T` swapped on the main menu, that `E`, `K` and
  `P` mean something different in the mailbox, and that settings and list
  rows are chosen by two-digit numbers.
- **MANUAL — redraw main-menu art** that draws `C[o]mmunities` or
  `S[t]aff list`. Until then those items are blanked and listed in `{menu}`.
  Art for other screens that draws old keys (`De[l]ete`, `[N]ext`, a
  one-digit row number) is not checked and will mislead until redrawn.
- **MANUAL — check scripts and notes that name keys.** Anything that drives
  the console or a caller screen by keystroke, or tells people which letter
  to press, must use the new keys and two-digit row and field numbers.
- **MRC lines with a decorated handle look different** for callers who keep
  the default, **combined**. Earlier lines do not change.
- **MANUAL — choose the node page.** A node with a managed name will have a
  public page once the pages go live, shown but not indexed by default. To
  have none, set **Managed DNS → `[W]eb page`** to off. To list public boards,
  your guest account must be able to read them; to list none, leave guest
  login off.
- **Nodes start signing the new descriptor fields** from the first sync
  after the upgrade; nothing to do on the peers' side.

**Rolling back needs a restore.** A 7.17.1 wheel refuses a schema-123
database ("database schema version 123 is newer than this NetBBS build
supports (120)"). **MANUAL — to roll back:** stop NetBBS, install the 7.17.1
wheel, then restore the backup taken before the upgrade. Anything since the
upgrade is lost with it. After the rollback:

- **A node page set to off comes back.** 7.17.1 cannot send `node_page`, so
  the next descriptor reads as the default and the page is published again
  (once the pages are live).
- **The old keys are back,** and callers who learned the new ones meet the
  old ones again. If you upgrade again, migration 122 marks every account
  once more, so everyone sees the notice again.
- **MRC lines received since the upgrade go with the restore;** 7.17.1
  shows every older line as label only.

## Verification boundaries

- **The node pages are not live.** The setting, the export (#1167), the
  generator (#1168, #1174) and the badge (#1169) are merged and tested;
  the deploy (#1165 step 4, prepared in the ops repository but not applied)
  needs this release on ReLink first. Until then `https://www.netbbs.org/~<name>`
  does not answer, although the Managed DNS screen already shows the address.
  No page has been built from ReLink's real node map or the live
  registrations database. A headless render matched the website at 1100
  columns; the 390-pixel (phone) render was not checked.
- **Mixed-version Link** was checked by reading v7.17.1's descriptor
  handling (signature over the whole envelope, no field list), not by a
  session between a 7.17.1 node and a 7.18.0 node.
- **The key changes** are tested with scripted sessions, source checks,
  and the Telnet/SSH key reader and web terminal (`tests/test_reserved_keys*.py`,
  `test_help_everywhere_*.py`, `test_first_letter_keys.py`,
  `test_row_numbers.py`, `test_keys_notice.py`). They have not been walked
  in SyncTERM or another real terminal for this release, and the website's
  screen captures still show the old keys.
- **Styled MRC names** are tested on recorded and constructed lines; how the
  combined style splits handles from other MRC clients in live hub traffic
  has not been checked.
- **The release gate:** the full suite (`pytest -n auto`) on PR #1175's
  tree: 14,647 passed, 139 skipped, 55 failed under load. All 55 (War Dialer
  real-process, door runtime, MRC wire, lifecycle, chat timing and one test
  that expected the old fullscreen-editor hint) were rerun on their own on
  the release tree and pass, after the hint test was updated; the
  `timing_sensitive` tests pass 5/5. The two commits after that suite run
  (the editor's key reader keeping its name, a handbook and test wording
  fix) were covered by their own targeted runs, not a second full suite.

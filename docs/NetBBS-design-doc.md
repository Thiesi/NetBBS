# NetBBS architecture and product design

This document is the **current normative design** for NetBBS. It describes what
the system means, what users and node operators may rely on, and the boundaries
future implementations must preserve.

It is not a chronological decision diary. The former numbered sign-off rounds,
superseded alternatives, corrections, and intermediate implementation status
remain available through Git history. Do not reconstruct that chronology here.

Use project sources in this order:

1. this document for product, protocol, authority, and long-lived UX decisions;
2. current GitHub issues for unresolved work and acceptance criteria;
3. `docs/NetBBS-worklog.md` for durable implementation constraints and lessons;
4. source, migrations, tests, and Git history for exact implementation detail.

When these sources disagree, investigate and update the stale source. Do not
choose whichever answer is most convenient.

This ordering is for developers changing NetBBS. A SysOp running a node, not
modifying it, wants `docs/NetBBS-operator-guide.md` (install/deploy/upgrade)
and `docs/NetBBS-SysOp-Handbook.md` (day-to-day administration) instead —
both stay consistent with the normative decisions here, but neither requires
reading this document first.

## Current status

- Phases 1 and 2 are complete as working standalone BBS software.
- The post-Phase-2 local additions—Communities, identity attestation,
  asynchronous personal mail, and self-update foundations—are substantially
  implemented.
- Phase 3 is active. NetBBS Link has real identity, canonical event encoding,
  authenticated HTTP transport, persistent peer/event state, seed and peer
  discovery, linked boards, tier-1 Link messages, outgoing-only-node relays,
  and deterministic multi-node fault testing.
- Phase 3 remains **private and experimental federation**. Phase 4 trust,
  reputation, and quarantine are the public-federation readiness gate.
- Later phases—real-time Link chat, advanced Link governance and Link
  Communities, and door-game compatibility—remain future work.

Implementation status belongs beside the relevant design rule below and must be
updated in place. Do not append victory narratives or test-count snapshots.

---

## 1. Product identity, terminology, and principles

### 1.1 Names

- **NetBBS** is the software project.
- **NetBBS Link** is the decentralized network connecting NetBBS nodes. “The
  Link” is acceptable informal shorthand.
- **Board** always means a message board.
- **Area** always means a file area. Never call a file area a board.
- **Link** prefixes features which exist specifically because of NetBBS Link:
  **Link message**, **Link Community**, **Link-wide** presence or chat.
- An ordinary local resource which participates in NetBBS Link keeps its normal
  noun with the adjective **linked**: linked board, linked channel, linked file
  area. Do not rename such resources into “Link boards” or similar proper
  nouns.

### 1.2 Foundational principles

NetBBS Link is foundational, not an add-on. Every durable local feature should
be designed with its possible network extension in mind, even when the local
version ships first.

The standalone BBS must remain complete and useful without NetBBS Link. Local
packages must not import Phase-3 federation code merely to perform ordinary
local work.

Node sovereignty is non-negotiable:

- no master node exists;
- no node, moderator, or majority vote can force another operator to store,
  display, or delete content;
- carrying remote content is always a local decision;
- moderation and trust signals may propagate, but enforcement remains local;
- remote closure or suppression events cannot remotely erase bytes already
  stored by another node.

The design prefers correctness, explicit authority, bounded resource use, and
visible failure over cleverness or silent degradation.

### 1.3 Non-goals

NetBBS is not intended to:

- recreate a centralized social network behind a terminal interface;
- promise anonymity from a user’s own home-node operator for ordinary
  password-only accounts;
- make every local feature network-wide immediately;
- replicate every file byte to every node;
- hide unresolved trust decisions behind signature verification alone;
- preserve historical BBS protocol constraints when they conflict with the
  native NetBBS model.

---

## 2. Platform, architecture, and scale

### 2.1 Target platform and stack

**Platform support tiers (issue #81).** Without an explicit contract,
portability becomes accidental — a NetBSD-specific detail creeping into
core code, or Linux/macOS breakage going unnoticed, or (the opposite
failure) primary-target constraints getting quietly weakened to
accommodate a platform that was never meant to drive architecture.
Four tiers, in order:

- **Tier 1 / primary — NetBSD.** Every design and dependency choice must
  work here; this is the platform "does this work?" defaults to. NetBBS
  itself is obtained and updated only from its official GitHub releases
  or tagged source; shipping NetBBS through pkgsrc or another external
  package manager is neither planned nor supported. pkgsrc remains the
  preferred source for external dependencies on NetBSD. Existing
  Tier-1-driven decisions include PyNaCl/libsodium for core identity and
  FTS5 availability confirmed by tracing
  pkgsrc's actual `lang/python312` → `databases/sqlite3` build chain
  (worklog §6), not assumed.
- **Tier 2 / supported — mainstream Linux distributions**, using
  ordinary Python packaging (venv/pip) and system service managers
  (systemd). A regression here is a real bug — *unless* fixing it would
  weaken a Tier-1 constraint, in which case Tier-1 wins; Tier 2 never
  gets to relax what Tier 1 needs.
- **Tier 3 / best-effort — other POSIX systems** (macOS, FreeBSD,
  illumos, etc.), until a maintainer or user demonstrates a build/run
  exercised with some regularity. No dedicated design effort beyond
  staying ordinary POSIX-portable Python.
- **Development-only compatibility — Windows.** Convenient for local
  development and testing (this project's own dev sandbox routinely
  runs here) — never a target for production semantics that depend on
  POSIX facilities: real signal delivery (`asyncio.loop.
  add_signal_handler`), `os.kill(pid, 0)`-style liveness probes, POSIX
  file permission bits, `termios`/`tty` raw-mode terminal control.
  Existing platform branches — `netbbs.net.local_terminal` (raw-mode
  input), `netbbs.backup._process_is_running` (restore's liveness
  probe), `netbbs.__main__`'s signal-handler setup — already draw
  exactly this line, each with its own `sys.platform`/`os.name` check
  and a comment naming Windows as the dev/test fallback, never the
  deployment target; this tier list makes that existing practice an
  explicit policy rather than an implicit one three separate modules
  happened to agree on.

Consequences of the tier list, not separate rules:

- A new dependency is evaluated against the Tier-1 target *before*
  adoption: is it available through pkgsrc, does it need an unusual
  compiler toolchain, or does it assume behavior unlikely to hold on
  NetBSD? We make a strong effort to choose dependencies available in
  pkgsrc so a NetBSD deployment remains straightforward. Tier-2/3
  convenience is never sufficient justification by itself.
- Platform-specific code stays isolated in a small number of narrow
  modules/functions (the three named above), never scattered
  `sys.platform`/`os.name` checks through domain code. Core/domain/
  protocol modules (`netbbs.boards`, `netbbs.link`, etc.) are plain
  POSIX-portable Python with no platform branching at all.
- Installation/service examples (issue #82) use GitHub-hosted NetBBS
  releases on every platform and cover Tier 1 (NetBSD/rc.d) and Tier 2
  (Linux/systemd) explicitly; a Tier-3 or
  development-only path is documented as such, never presented as an
  equally-supported option.

- Runtime: Python 3.11+ with asyncio.
- Storage: one SQLite database per node, using WAL mode.
- Core cryptography: PyNaCl/libsodium. The optional SSH transport uses
  AsyncSSH and therefore `cryptography`; its source build on NetBSD needs
  Rust, a C compiler and Python headers, OpenSSL and libffi headers, and
  build-discovery tooling. Keep that toolchain isolated to the optional
  feature and avoid adding comparable build burdens without compelling
  benefit.
- User transports: Telnet, SSH, and web/xterm.js.
- Asynchronous Link transport: signed HTTP+JSON.
- Future real-time Link chat transport: Noise Protocol Framework.

### 2.2 Modular boundaries

The system is a modular package, not a monolithic script.

- `netbbs.auth` owns accounts and authentication.
- `netbbs.identity` owns cryptographic identities and addressing.
- `netbbs.boards`, `netbbs.files`, `netbbs.chat`, and `netbbs.mail` own local
  domain state.
- `netbbs.communities` owns Community state and inherited-value resolution.
- `netbbs.moderation` owns shared authorization and audit primitives.
- `netbbs.rendering` owns ANSI, reflow, screen-buffer, and editor-independent
  rendering behavior.
- `netbbs.net` owns user-facing flows, sessions, and transport orchestration.
- `netbbs.link` owns Link events, protocol, transport, persistence, discovery,
  synchronization, relaying, and local-to-Link bridges.
- `netbbs.storage` owns migrations, database connections, and execution lanes.

Domain functions are normally synchronous and `db`-first. Async session or
network code dispatches blocking work through a `DatabaseLane`. Link bridge
modules may depend on local domains; local domains must remain Link-unaware.

Rendering, protocol, storage, and transport concerns remain distinct. A generic
transport must not learn every event’s product semantics, and domain storage
must not decide how terminal output looks.

### 2.3 Expected scale

The primary deployment remains one modest self-hosted node operated by one
SysOp. The architecture targets:

- dozens to low hundreds of concurrent interactive sessions;
- small-to-medium Link deployments initially;
- correctness across multiple nodes even before large deployments exist.

SQLite is appropriate at this scale. The first expected scaling pressure is
write contention and queued background work, not raw interactive connection
count. Scale decisions beyond demonstrated workloads must be based on measured
behavior using the deterministic multi-node harness, not estimates alone.

---

## 3. User connectivity, rendering, and interaction

### 3.1 Connection methods

Telnet, SSH, and web/xterm.js are first-class user transports. Product behavior
should be transport-independent unless a capability genuinely requires a byte
stream, browser code, or another transport-specific primitive.

### 3.2 Rendering model

Use hybrid terminal rendering:

- ordinary screens use ANSI/VT100 text with reflow;
- cursor-addressed screen-buffer rendering is reserved for interfaces which
  benefit from it, such as fullscreen editors and pinned chat rows;
- the minimum supported terminal is 40x24;
- screens must degrade clearly rather than corrupting output when a terminal is
  too small or lacks a capability.

Untrusted user text is sanitized before styling. Trusted ANSI is added only
after sanitization. Nested colored fragments are composed independently because
an SGR reset does not restore an outer color.

A SysOp may override three of the node's branding colors -- accent (board/
channel/user names and other navigable-item branding), header (section
titles and frame borders), and clock (the main-menu prompt's time display)
-- independently (issue #162, part three of the skinning initiative the
welcome banner/main-menu masthead already started). `netbbs.net.node_theme`
resolves each node-wide RGB override (downgraded to the nearest 256-color
index for a session without truecolor support) in place of the matching bare
`theme.py` constant everywhere a screen renders one, including every shared
rendering primitive (`screen_title`, `double_frame`, `empty_state`) and every
screen built on top of them. A SysOp sets or clears each color from
Settings > [C]olors, which previews the candidate RGB against real sample
text at both truecolor and 256-color depth before asking for confirmation.
This is deliberately narrow: every *semantic* color in `netbbs.rendering.
theme` (errors, warnings, success, privilege badges, operational alerts,
verified-identity badges) stays fixed everywhere, never SysOp-configurable --
a caller who has used several NetBBS nodes can keep trusting that red always
means failure and green always means verified/success, regardless of any
node's own branding. Full palette theming (every color configurable) was
considered and rejected on exactly this basis, not merely deferred as too
large -- see issue #163.

A SysOp may additionally give the node name itself -- the breadcrumb segment
shown in the upper-left corner of every screen -- a per-character gradient
(issue #175), the same flair the default welcome banner's own wordmark
already gets, from Settings > Node Name > [G]radient. This is a different
kind of override from the three branding-color slots above: it recolors one
specific piece of text, not a semantic color slot standing in for a theme
constant, so it's a fixed preset list (`netbbs.rendering.gradient.
GRADIENTS`'s own keys) rather than a fourth RGB slot. It's also resolved
once at login and cached on `Session`, unlike the three RGB slots' own
per-screen `db` lookup -- it shares `node_display_name`'s existing
resolve-once lifecycle instead, since a SysOp's own change should take
effect for new connections the same way a rename does, not live-update a
session already in progress.

The welcome-banner/masthead mechanism extends to three more session-
lifecycle points (issue #177): a logoff banner, shown above the ordinary
"Signed out"/"Goodbye!" message on an intentional Log off only (never on
an idle timeout, kick, or account revocation); and a pair of new-account
banners bracketing self-service registration, one shown once before the
signup workflow begins and one once it completes successfully (covering
both an immediate login and a pending-approval outcome). Both Telnet/web
and SSH's own, separately-implemented registration paths show the
before/after banners; SSH's version reaches them through `send_auth_
banner` (before) and a kbdint challenge's own `instruction` field
(after) rather than an interactive screen, since SSH authenticates at
the protocol layer with no such screen of its own. Each of the three is
its own independent singleton, reachable from Settings > Session
banners, with no built-in default art -- disabled is a complete
non-event, unlike the welcome banner's own always-renders-something
default.

The main-menu masthead (issue #161) also extends to the three top-level
index/listing screens -- board list, file areas, and the chat channel
picker (issue #176) -- since each renders once per view as a
`screen_title` + listing through the shared `netbbs.net.picker.
pick_item`, structurally identical to the main menu despite being a
recursive, categorized/Community-scoped browsing hierarchy rather than
one flat screen. Each masthead shows at *every* level that hierarchy
reaches (the unfiltered top level, a category, a Community/Uncategorized
scope), not only the very first screen -- it marks "you're in this
section," not one specific screen state. This required `pick_item`
itself to grow a `masthead` parameter (threaded through its own internal
redraw closure so the masthead survives paging/search/sort/refresh, not
just the first paint) rather than each of the three call sites prepending
it independently, since `pick_item`'s live picker redraws itself
wholesale on every state change, unlike the main menu's own single
per-loop-iteration draw. Deliberately never the per-board/per-area
drill-down screens or the inside of a live chat channel -- see issue
#176's own scoping discussion for why those are a bigger feature and a
categorically different rendering model, respectively, not simply "one
level deeper." Each of the three is its own independent singleton,
reachable from Settings > Section mastheads, with the same no-default-
art/complete-non-event-when-disabled shape issue #177's own banners use.

The project intentionally provides two composition paths:

- a robust simple/line-oriented editor available everywhere;
- a nano-like fullscreen prose editor as a convenience preference.

The line-oriented path is a real editor, not merely repeated irreversible
prompts: callers can revisit, insert, replace, and delete already-submitted
logical lines. Composition is separate from commitment. Mail and new posts
show a final review state from which the caller may revise address/subject/body
as applicable, commit explicitly, or cancel; leaving either editor never sends
or posts by itself. Fullscreen-editor output passes through the same review
boundary so editor preference cannot change send/commit safety.

Board post composition (new posts and edits) additionally distinguishes
discarding from saving: `/cancel` (line editor) or discarding (fullscreen
editor) always deletes any in-progress draft; `/exit`/`/quit` (line editor)
or "Keep draft & exit" (fullscreen editor) instead save it to the same
per-caller autosave target the fullscreen editor already used for crash
recovery, and return without committing. Board entry proactively offers to
edit, delete, or ignore a saved new-post draft for that board before the
ordinary post list/navigation flow; re-opening a specific post for edit
offers to resume its own saved draft the same way the pre-existing
crash-recovery prompt already did. Mail composition and other callers that
never opt into a draft target keep exactly the old discard-only behavior --
`/exit`/`/quit` are not recognized there at all.

In-context help is a single shared rendering primitive
(`netbbs.net.help_overlay.show_help`) reused by two different key
conventions rather than one universal key: Ctrl+G inside the fullscreen
prose editor (nano's own Help convention, listing its keybinds and
explaining save-draft/resume), and Ctrl-H at ordinary hotkey-menu SysOp
screens built on the shared draft-based field editor
(`netbbs.net.resource_editor`), showing whichever fields on that screen
have help text authored. The two keys differ because Ctrl-H and Backspace
share one byte (0x08): safe to repurpose at a single-keystroke menu, where
Backspace already has nothing to act on, but not inside a real text editor,
where 0x08 is live backspace-editing. Authoring help text per field is
incremental, not required for every field up front.

Ctrl-C (confirmed with Thiesi, dogfood question) is an *incremental*, not a
universal, cancel key: `char_input.read_key()` returns it as a distinct
`CANCEL_KEY` sentinel, the same "return a distinguishable value, let call
sites opt in" shape `REDRAW_KEY`/`REFRESH_KEY`/`HELP_KEY` already use,
wired in screen-by-screen wherever an existing cancel affordance
(`[B]ack`, `[C]ancel`) already exists rather than swept across every
prompt at once. Deliberately does not touch `read_line()`'s editable
path in this pass -- unlike Backspace's byte, Ctrl-C during real
free-text entry has no single safe meaning across every caller (a bare
blank line already means something different per caller, e.g. "finish
and review" in the line editor, not "cancel"), so real-text-entry
cancellation is left for a later, separately-scoped increment. A
screen with no cancel affordance at all, or one that hasn't adopted
this yet, simply bells for Ctrl-C like any other unrecognized key.

Conventional yes/no prompts act on one key: `Y`/`N` immediately, or Enter for
the displayed default/current value. This uses a confirmation-specific input
primitive; generic single-key menus retain their deliberate rule that Enter is
not a menu action. Unsupported confirmation keys are rejected rather than
silently selecting a default.

Color is semantic rather than decorative state: labels, values, actions,
metadata, warnings, and success/failure states use shared theme roles across
screens. Truecolor is progressive enhancement with a deliberate 256-color
fallback, never a requirement for understanding a screen. Product polish is
bounded by named mature surfaces rather than an open-ended theming rewrite.

ANSI art editing and prose editing are separate concerns. Syntax highlighting,
spell checking, and similar enhancements remain optional modules rather than
core editor assumptions.

### 3.3 Product presentation and default visual identity

Presentation work proceeds as bounded vertical product increments rather than
waiting for every later roadmap phase. NetBBS ships one intentional default
visual identity before it grows an arbitrary theme engine. The default should
feel like a modern terminal application with BBS character: restrained color,
clear hierarchy, generous spacing, compact panels and badges, and recognizable
NetBBS branding rather than a wall of prompts or ornamental ANSI everywhere.

Ordinary screens share a small rendering vocabulary: title/breadcrumb,
sections, responsive menu grids, metadata, status messages, empty states, and
action hints. These primitives return styled terminal strings and never absorb
domain behavior. A screen should make location, content, and available actions
clear at a glance.

Layouts target 80x24 as the classic baseline, use additional width when
available, and collapse to one column at the 40x24 minimum. ASCII structure is
the universal baseline because Telnet terminal encodings vary; Unicode box
drawing must not be required for correct layout. Truecolor remains progressive
enhancement. Motion, gratuitous full-screen clearing, and decoration which
delays interaction are avoided.

The first-impression surface is one coherent product slice: generated default
banner, login, authenticated greeting, and home menu. Later increments apply
the same vocabulary to Communities/boards, mail/files/search/directory, chat,
and the SysOp operations console. Every increment covers narrow, ordinary, and
wide terminals plus empty, populated, warning, and error states.

### 3.4 SysOp operations console

The SysOp entry point is an operational control center, not a flat catalogue
of administrative forms. Its landing view summarizes the running node's mode,
active sessions, Link health, moderation queues, backup and update recency,
outbound failures, and recent Link diagnostics using concise semantic status.
The standalone admin CLI renders the same view but states clearly that live
node controls are unavailable rather than pretending the process is online.

Navigation separates four operator intents: users, content, operations, and
settings. Operations contains observation and intervention for the live node,
Link, outbound work, diagnostics, recovery, and backups; settings contains
durable configuration such as presentation, update checks, timestamps, and
trust policy. Context-sensitive quick actions may lead directly from the
landing view to node, Link, outbox, and backup screens. Hidden capabilities are
not advertised when their runtime context is unavailable. The dashboard can be
refreshed explicitly, and action screens return to the console without losing
the operator's place.

Status context in the console is deliberately two-tier (issue #206). The five
top-level consoles — Users, Content, Operations, Settings, Node — each show a
full panel of what's actually relevant there: live counts, health badges, or
current configuration values. Every nested screen beneath them that has no
such panel of its own instead shows one condensed status line carrying just
two facts obtainable without live node/session/Link state: last backup and
last update-check outcome. This keeps recovery-relevant context visible while
an operator is deep in a nested screen without threading live node state
through call chains that don't otherwise need it, and without re-deriving a
richer panel nested screens have no room to show. A screen that already has
its own full panel does not also show the condensed line.

---

## 4. Accounts, authentication, identity, and addressing

### 4.1 Account authentication

Local users may authenticate with:

- username and password, the default path;
- an optional personal Ed25519 keypair for passwordless challenge-response
  login.

The server never needs a personal user private key. Password-only users are
expected to remain the majority and must not be treated as second-class users.

### 4.2 Registration modes

A node has one registration mode:

- `open`: self-registration creates an immediately usable account;
- `approval_required`: self-registration creates a pending account which
  cannot authenticate until approved;
- `closed`: the public registration option is absent and accounts are
  SysOp-created.

Registration determines whether an account may exist and log in. Link
probation and reputation determine what an active identity may do; these are
separate axes.

### 4.3 Account levels and the usable-SysOp invariant

One integer level drives ordinary level gating. `SYSOP_LEVEL = 255` is the
reserved top level; SysOp is not a parallel role flag.

Promote, demote, disable, enable, approve, and hard-delete operations must never
leave the node with zero **usable SysOps**. A usable SysOp:

- has level at least `SYSOP_LEVEL`;
- is not disabled;
- is not pending approval.

The invariant is enforced transactionally against fresh database state, not
against a stale object supplied by a caller.

Hard deletion preserves content provenance through denormalized display labels
or nullable author/uploader references. Personal access rows and private state
which cannot meaningfully outlive the account are deleted according to explicit
foreign-key policy.

### 4.4 Human-facing Link addresses

The normal human-facing cross-node address is:

`user@friendly-name` or, where a friendly name is ambiguous,
`user@canonical-dns-name`

Link endpoint descriptors carry both claims inside the node-signed hello
bundle. User-facing screens show the friendly name, qualified by the canonical
DNS name where useful, and do not expose fingerprints by default. DNS names are
unique routing names, but neither a friendly name nor DNS is cryptographic
identity authority.

The underlying address and every protocol/persistence relationship remain
`user@node-fingerprint`. Durable linked content -- channel scrollback, mail,
carried board posts, fetched Link files -- therefore persists the fingerprint
and resolves the home node's *current* friendly identity when rendered, so a
benign rename is followed and nothing stored has to be rewritten. A node with
no authenticated profile (one that has never been admitted, or is known only by
an administratively configured fingerprint) is shown by that fingerprint rather
than a shared placeholder. A full fingerprint is available as **Technical
identity** in the relevant SysOp detail view and remains accepted as an
advanced/backward-compatible input. Friendly-name resolution must be unique;
an ambiguous presentation name is refused with a request to use an unambiguous
DNS name or the technical identity. DNS and friendly claims share that one
namespace, so a reference matching one node's DNS claim and another node's
friendly claim is ambiguous rather than silently preferring either. Friendly
names are compared in one Unicode normalization form (NFC), so canonically
equivalent spellings are one name, never two claims. UI delimiters, invisible
control/format characters, and the `Unnamed linked node` fallback label are
reserved and cannot be claimed as friendly names.

Peers retain authenticated observations of all three values. A friendly-name
change under the same fingerprint is an informational continuity notice. A DNS
change under the same fingerprint is a more prominent routing notice. Reuse of
a familiar friendly or DNS name by a different fingerprint is a strong
cryptographic-identity warning: the UI explains that recovery/replacement may
be legitimate but impersonation is possible, and it does not prevent the user
from continuing. Presentation names never transfer trust or reputation between
fingerprints.
An undismissed cryptographic-identity warning continues to be shown at
interaction boundaries even if a newer benign profile change is observed;
only SysOp acknowledgement dismisses it. Bounded observation pruning therefore
retains undismissed security warnings ahead of newer benign observations. The
local node likewise retains a bounded history of its previous friendly and DNS
claims so another fingerprint cannot adopt a just-renamed local identity without
raising the same warning.

### 4.5 Identity tiers

NetBBS has three author/identity tiers.

#### Password-only user

A password-only user has no personal cryptographic identity. Link events use a
`node_vouched_user` author reference containing:

- the home-node fingerprint;
- an opaque local user identifier.

The home node signs on the user’s behalf. Key rotation and recovery are entirely
the node operator’s responsibility.

#### Personal-key user

An opt-in user may register one or more keypairs (issue #222 — multiple
simultaneous personal keys/devices, shipped) for passwordless login, each
independently valid; none is a root/operational hierarchy the way node
identity has. Author-identity purposes elsewhere in this design (Link
event authorship, display) that need exactly one fingerprint per account
use the account's *primary* key — the first one registered, or another
automatically promoted if the primary is later removed while other keys
remain.

**Promotion is a mechanical fallback, not an identity claim (code review
follow-up, PR #225).** There is no signed key-transition chain linking a
newly-promoted key to the one it replaced — unlike node identity, whose
root key signs each operational-key transition specifically so remote
peers can verify continuity across a rotation. A promoted personal key
is, from every remote Link peer's point of view, simply a different,
unrelated fingerprint: content authored after a promotion is *not*
cryptographically provable as continuing the same reputation history as
content authored before it, even though the local account is unchanged.
This is the same limitation the single-key model already accepted below
("losing the key loses that key-based identity and reputation
continuity"), now reachable as a side effect of removing one key among
several rather than only by losing your one and only key — a real,
disclosed gap, not a hidden one, and one this feature does not attempt
to close (a signed personal-key transition chain, mirroring node
identity's, is a real future option if reputation continuity across a
personal key change ever becomes a stated goal — not built now, not
implied by anything here).

There is no bespoke recovery mechanism for any individual key. Losing
every registered key loses key-based identity and reputation
continuity; the local account may still use ordinary account recovery
policy.

#### Node identity

Every node has:

- one long-lived root key whose fingerprint is the stable node identity;
- one operational signing key for events and content;
- one operational transport key reserved for Noise-based real-time transport.

The root authorizes and revokes operational keys through signed transition
records. Historical signatures remain verifiable by walking the transition
chain back to the root.

Routine operational-key rotation and compromise response do not change the
node address. Root-key loss or compromise has no cryptographic recovery in the
current design. Social/M-of-N recovery remains a possible future extension, not
an assumed capability.

Replacing the root identity necessarily changes the fingerprint and is treated
as a new cryptographic identity. Keeping the same friendly or DNS name makes
that replacement recognizable to humans but does not establish continuity;
peers raise the strong warning above and continue to permit interaction.

Root and operational keys are generated at initial bootstrap. Rotation is a
guided SysOp action. Root-key custody is part of ordinary node backup and
restore rather than requiring an HSM or offline ceremony.

---

## 5. Authorization, moderation, and identity attestation

### 5.1 Resource gates

Boards, file areas, channels, and Communities may apply:

- minimum user level;
- minimum age;
- verified-name requirements;
- visibility and membership policy appropriate to the resource type.

Resource-level scalar settings are nullable:

- `NULL` means inherit the containing Community’s default, if any, otherwise
  use the system default;
- an explicit value, including `0` or `none`, overrides inheritance.

A Community default is a default, not a mandatory floor or ceiling. A child
resource may currently loosen or tighten it explicitly.

### 5.2 Moderator authority

Boards and file areas distinguish read and write access. Channels use a join/
participation gate rather than asynchronous read/write separation.

Moderator permissions are composable primitives such as read, write, edit,
delete, approve, manage members, mute, ban, and topic control as appropriate.
Moderators need not be SysOps.

Authority scopes are:

1. per-object;
2. Community-blanket, applying to present and future matching resources in one
   Community;
3. local-blanket, applying to local-only matching resources on one node;
4. Link-blanket, applying to linked matching resources carried by a node.

Link-blanket authority does not imply local authority. A person who needs both
must receive both explicitly.

Only a SysOp can grant or revoke blanket authority or change node
configuration. A suitably authorized Link-blanket moderator may initiate a new
linked resource, but:

- the node identity signs and owns the genesis event;
- the initiating human is recorded separately for audit;
- initiation grants no power to appoint further blanket moderators or alter
  unrelated resources.

Every moderation action is audited. Moderator changes to immutable Link content
must be represented as new authorized events, never silent mutation.

### 5.3 Board and file moderation

A board or file area may require approval before new posts/uploads become
visible. Local maintenance follows:

`active -> expired -> deleted`

with a grace period between expiration and deletion. Pin and expiry-exemption
currently use the existing edit permission. Local pruning never becomes a
network-wide deletion instruction.

### 5.4 Channel visibility and membership

Channel visibility and join policy are separate:

- listed or hidden;
- open to otherwise eligible users or members-only.

`hidden + open` is permitted but is obscurity, not access control.

Local invitations may be immediate for online users and retained with expiry
for offline users. Membership persists until revoked unless a channel defines
otherwise. Linked-channel membership eventually becomes signed governance;
it is not represented as end-to-end confidential from participating node
operators.

### 5.5 Identity attestation

Age and verified-name policy is local and jurisdiction-specific. NetBBS provides
mechanism, not a universal legal definition.

Users may provide nullable, independently visible:

- birthdate;
- display name;
- location;
- other profile fields.

Age is computed from birthdate at check time. It is never stored as a derived
current age. If a resource has an age gate and no usable birthdate or verified
age attestation exists, access fails closed.

A `user_attestation` records:

- subject user;
- attribute (`age` or `name`);
- attested value;
- verifier identity;
- signature;
- creation time;
- Link-visibility preference.

A verifier may use a personal key, or the node may vouch for a password-only
verifier. Verified values take precedence over self-reported values.

`can_verify_identity` is a separate SysOp-granted boolean, not another content
moderator tier.

Name requirements are:

- `none`;
- `verified`, requiring a verified name without compulsory display;
- `verified_and_displayed`, requiring resource-scoped visible disclosure.

A verified name never overwrites the user’s self-chosen display name. When a
resource requires display, render:

`display_name_or_username (=Verified Real Name=)`

The complete `(=...=)` unit uses a dedicated trusted color. The `=` marker
remains visible when color is stripped or inaccessible. User-controlled display
names may not contain the reserved `=` marker, and untrusted text cannot inject
ANSI styling.

Disclosure is resource-scoped. A resource which requires visible identity must
not cause the real name to leak into unrelated screens.

Remote propagation of attestations requires:

- the subject’s explicit opt-in;
- Phase-4 trust rules allowing the receiving node to decide whether to trust the
  remote verifier.

Link visibility is per attested attribute, independent of profile-field and
verified-badge visibility, and defaults off. Re-verifying an attribute resets
its Link visibility to off so consent for an old value never silently carries
onto its replacement. A node exports only a currently Link-visible local
attestation and signs a `remote_identity_attestation` object containing its
stable issuer fingerprint, the subject's `node_vouched_user` identity pair,
attribute/value, explicit opt-in assertion, issuance time, and expiry. The
maximum active lifetime is 365 days. Revocation is a separate signed
`remote_identity_attestation_revocation` object naming the exact original
content ID; neither expiry nor revocation deletes the signed historical row.

Receiving nodes verify canonical bytes with the issuer's currently authorized
operational signing key before persistence. Acceptance then remains purely
local and attribute-scoped: an explicit attestation-authority grant, its
`age`/`name` scope, the issuer node's current identity-integrity trust state,
expiry/revocation, and an optional reasoned SysOp accept/reject override produce
a persisted effective projection. Reporter/vouch configuration grants no
attestation authority. A manual accept may select a current valid signed record
from an otherwise unconfigured issuer, but cannot resurrect an expired or
revoked record. Ordinary callers see only whether the gate is met; issuer
configuration, notes, and override reasons remain SysOp-only.

Remote real-name rendering uses the same trusted `(=...=)` unit as local
attestations and only within a resource requiring
`verified_and_displayed`; accepting a remote attestation never exposes that
value in unrelated screens.

A carrying node may always apply its own local attestations to its own users
when enforcing a carried resource’s local age/name policy.

---

## 6. Local product domains

### 6.1 Message boards

Local boards provide:

- categories and stable navigation IDs;
- posts and replies;
- moderation and pending approval;
- expiry, pinning, and exemption;
- immutable revision history for edits;
- simple and fullscreen composition.

Read/unread state, follows, activity discovery, and local search across
boards, file areas, channels, and Communities are specified together in
§6.6, not per-domain.

A visible edit is a revision, not destructive replacement of history. Any
threading or revision semantics which affect Link event IDs or propagation must
be settled in Phase 3; only presentation refinements may wait until Phase 7.

### 6.2 File areas

Local file metadata lives in SQLite; file bytes use content-addressed filesystem
storage. Areas support permissions, moderation, expiry, and Zmodem transfer on
byte-capable transports.

File bytes are node-local. NetBBS Link will distribute catalogue/descriptor
information and fetch content on demand in bounded resumable chunks. It will
not replicate every file to every node.

### 6.3 Real-time chat

Local chat is typed event traffic, not preformatted strings. Initial event types
include:

- ordinary message;
- `/me` action;
- online private message;
- join/leave;
- alias change;
- system notice.

An optional `/nick` alias is presentation metadata only. Every context retains
the authenticated canonical identity, and permissions, moderation, blocking,
reputation, and addressing always use canonical identity.

Local chat includes bounded persistent channel scrollback, presence, away
state, invitations/membership, `/who`, `/whois`, `/names`, `/list`, `/join`,
`/leave`, `/topic`, completion, and online private conversation.

`/msg` and `/private` remain ephemeral and online-only. They never silently
fall back to asynchronous mail.

A separate mutual invite/accept direct chat also exists, alongside `/msg`/
`/private` rather than replacing either: unlike both (one-off, or a one-sided
redirect the target never agrees to), both sides must explicitly be in the
same room at the same time. Reachable from the Who screen (`[I]nvite to
chat`) or `/dm <user>` from an active channel. Exclusive with channel chat --
one active chat screen per session, the same scope Phase 2's one-channel-at-
a-time limit already establishes below. Fully ephemeral, the same as `/msg`/
`/private`: no persistence, no scrollback. An invite interrupts the main
menu live only when the recipient is idle there; otherwise it is shown the
next time they return to it, never inside an unrelated in-progress screen.
An unanswered invite expires automatically after a short fixed window, with
an explicit accepted/declined/timed-out outcome always shown to the inviter
-- never a silent no-op.

While the inviter waits, only `C` cancels; unsupported keys are rejected and
the invite remains live. If acceptance and local cancellation become ready in
the same scheduler turn, the already-committed acceptance wins so the accepting
peer is never stranded. Entering from `/dm` fully unwinds the channel screen
before direct chat takes ownership of session input/output, then reauthorizes
and re-enters the channel afterward. A peer-leave notice is a mandatory
lifecycle signal and uses priority delivery rather than lossy chat-traffic
overflow behavior.

The direct-chat pinned status row permanently exposes the leave command (with
a compact narrow-terminal fallback). Submitting a line clears/redraws the input
row before the committed chat line is rendered, so the sender sees one message,
not the input echo plus a second room copy. Identity labels and message bodies
are sanitized and styled as separate spans using semantic theme colors.

Phase 2 uses one active channel per session. Multiple simultaneous memberships,
background delivery, and Link-wide presence wait for Phase 5.

### 6.4 Personal mail

Local asynchronous mail is a persistent domain distinct from chat `/msg`.
Messages have sender/recipient views, subject, body, read state, and independent
delete state. The row is removed when neither side retains it.

Recipient mailboxes are bounded. When full:

- the oldest already-read message may be evicted to make room;
- unread mail is never silently discarded;
- if no safe eviction exists, delivery fails explicitly.

Local mail is the domain extended by Link messages; Link mail does not create a
parallel mailbox UI.

### 6.5 Communities

A Community is a topic-oriented coordination/container object above boards,
channels, and file areas. It does not merge those domains or change their
behavior.

Each board, channel, or file area has zero or one Community. “Uncategorized” is
the absence of a Community, not a synthetic row. Categories remain a separate
layer below Communities.

Communities provide:

- topic-first navigation;
- description and visibility;
- inherited level, age, and name-verification defaults;
- Community-scoped blanket moderator grants;
- a future unit for Link carry and governance.

The main navigation exposes:

- Communities;
- Uncategorized resources;
- Jump/search by resource type.

Each path leads to the same resource-type submenu and then the normal board,
channel, or area browser. Resources unrelated to Communities—mail, directory,
profiles, preferences, and administration—retain their own navigation.

Community-scoped category views must filter at the query layer so a category
used by resources in several Communities does not leak another Community’s
resources into the current view.

Deleting a Community:

- sets member resources to no Community;
- revokes Community-scoped blanket grants;
- shows the blast radius before confirmation.

Existing nodes migrate safely because the nullable Community reference leaves
all existing resources Uncategorized until a SysOp assigns them.

#### Link Communities

A Link Community is the same Community object announced through a signed Link
event, not a separate table or local type.

Two same-named Link Communities from different origins remain distinct.
Existing local Communities may be promoted into Link scope.

Carrying a Link Community is intended to carry its present and future member
resources by default, while retaining visible per-resource and whole-Community
local exclusions. Origin defaults are recommendations; carrying-node overrides
win locally.

Actual Link Community event schemas, signed membership changes, and advanced
governance are Phase 6 work.

### 6.6 Activity, unread state, follows, and search (issue #56)

A topic-first Community hierarchy is only more useful than a plain directory if
a user can tell what changed since their last visit. This section is the
complete answer to issue #56: read/unread semantics, follow state, a
new-activity surface, and local search. It replaces §6.1's earlier vague
"local search/navigation foundations" phrase.

#### Read/unread state

Local mail already has a complete, working model: a per-message `read_at`
timestamp, a live `unread_count` query, and independent sender/recipient
deletion. A delivered Link message is a normal row in the same table, so it
already has full read tracking the moment it lands in a mailbox. Nothing new
is needed for mail; issue #56's mail bullet is already satisfied.

Boards, file areas, and channels need a per-user, per-container **read
cursor**, not a per-item flag — per-item read state for a potentially
unbounded board would itself be an unbounded table. One new table holds it:
`(user_id, object_type, object_id)` primary key, where `object_type` is
`board`/`channel`/`file_area` and `object_id` is that resource's own local
integer id (the same id `community_id`/category columns already reference —
never the content-addressed `post_id`/`file_id`, which only identifies one
item, not a container). Its payload is the newest item's ordering key the
user has already seen:

- boards and file areas already page with a stable `(created_at, post_id)` /
  `(created_at, file_id)` keyset cursor (the existing `list_posts_page`/
  file-listing implementation) — the read cursor stores exactly that same
  tuple shape, so "what's unread" is the identical tuple comparison keyset
  pagination already performs for `after=`, just anchored at the user's own
  cursor instead of a page boundary;
- channel scrollback has no revision concept and is already ordered by a
  plain monotonic message id, so a channel's cursor is just that id.

An **edit never resets read state**: an edit's root post keeps the original
`created_at`/`post_id` (§6.1), which is exactly what the cursor comparison
keys on — a post a user has already scrolled past stays "read" after a later
typo fix, matching normal reader expectance. **Expiry and deletion cannot
corrupt a cursor**: an expired post keeps its `post_id` reachable until
nothing references it, and even final hard-deletion only ever removes an
already-fully-dereferenced row — a stored cursor value is a stable position
marker being compared against, never a live foreign key, so it cannot dangle
or resurrect deleted content.

A resource with no cursor row for a user has never been visited by them.
First visit — not a retroactive backfill — establishes the baseline: viewing
a board/file-area page or a channel's current scrollback advances that user's
cursor to the newest item they were just shown. This is also the complete
migration story for existing accounts (issue #56's last acceptance
criterion): the read-cursor table starts empty for everyone, including
existing users, at upgrade time. Nobody's history is scanned or backfilled;
the first real visit after upgrade sets the baseline, so only genuinely new
activity from that point forward counts as unread — never a flood of
years-old "unread" content on the first login after this ships.

A never-visited resource is surfaced as **not yet visited**, not as a
specific (and potentially enormous, meaningless) unread count — a real
numeric unread count only exists once a baseline cursor is established.

Channel scrollback is a bounded ring buffer (§6.3): a channel's cursor can
only ever express "unread among what's still retained." A message trimmed
out of scrollback before a user's next visit is simply gone, the same as it
already is for a session that was never connected to see it live — this is
an existing, accepted limitation of chat's ephemeral model, not a new gap
introduced here.

**Replies and mentions** need no new schema. A board post's existing
`parent_post_id` already names the post it replies to; "replies to me,
unread" is the same cursor-filtered query further restricted to posts whose
`parent_post_id` belongs to one of the user's own posts, run across every
board the user can read rather than one at a time. A channel "mention" is a
lightweight, unverified `@username` substring match against
`channel_messages.body` for messages newer than the user's channel cursor —
a convenience heuristic, not a structured or security-relevant feature; a
literal `@alice` typed with no intended addressee is an accepted false
positive, and a message directed at someone without using their exact
username is an accepted false negative.

**Node-local arrival order for carried content (issue #72).** The
model above compares a post/file's own `created_at` against the
cursor. That is correct for locally originated content, created in the
same order it becomes visible, but not for a Link-carried post: a
remote author's claimed `created_at` can be arbitrarily old if the post
only reaches this node after a partition or a delayed catch-up, and
comparing against it can let a genuinely new arrival silently sort
behind an already-advanced cursor. `posts`/`files` rows already carry a
second, distinct ordering with no schema addition needed: SQLite's own
`INTEGER PRIMARY KEY` rowid, assigned in strict insertion order
regardless of whether a row was created locally or materialized from a
carried Link event (the same property GitHub issue #68 already relies
on for edit-chain tie-breaking). `user_read_cursors` gains
`last_seen_arrival_id`, populated from that rowid; `unread_post_count`/
`unread_file_count`/`unread_replies_to` compare against it instead of
`created_at`, while `board_read_cursor`/`file_area_read_cursor` (feed-
position jump-to) are unchanged and still compare `created_at` -- the
two concerns use different orderings on purpose, per this section's own
distinction between authored chronology and node-local availability.
Existing cursors are backfilled from the post/file their existing
`last_seen_stable_id` already names, so an upgrade preserves exactly
what a user had already read rather than resetting anyone to
all-unread.

**Accepted scope boundary:** jump-to-first-unread can still land on the
board/area's ordinary newest page rather than navigating precisely to
an out-of-order arrival buried elsewhere in feed history, since the
jump cursor stays `created_at`-based. Unread *counting* and `[N]ew
scan`'s "has unread" detection are correct either way; only precise
jump navigation to that specific item is not yet solved. Reconciling
jump-to with arrival order, if ever wanted, is future work, not implied
by this fix.

#### Follows and favourites

Follow state is a new, separate table — `(user_id, object_type, object_id)`
where `object_type` is `community`/`board`/`channel`/`file_area` — deliberately
independent of every existing access concept it sits beside:

- **not** channel membership/invitations (`netbbs.chat.membership`), which
  govern *whether you may enter*, never *whether you care about it*;
- **not** node carry policy (`netbbs.link.boards.materialize_carried_board`),
  which is a per-node, all-or-nothing decision about whether Linked content
  exists locally at all, made with no per-user awareness whatsoever today;
- **not** Community membership, since a Community has no membership concept
  to begin with — it is a browsing/navigation container, not a joined group.

Following an object a user can no longer read (level raised, Community/
channel access changed, or — for a Linked board — this node stopping carrying
it) is never actively revoked; it simply stops being resolvable and is
filtered out of every follows-aware view at display time, the same
lazy-filter approach category/board listings already use elsewhere for
resources no longer visible.

#### Activity summary and direct jump ("new scan")

A single new main-menu entry — `[N]ew scan`, the traditional BBS term for
exactly this feature — is the fast, always-shown surface issue #56 asks
for, following the same unconditional-visibility
precedent `[J]ump to...` already sets.

New scan covers **every board, channel, and file area the user can currently
access**, not only followed ones — matching the traditional meaning of a
new-scan pass, and avoiding a chicken-and-egg problem where a brand-new
account has followed nothing yet and a "new scan" would show nothing at all.
Followed objects are surfaced first / distinguished within that same list; a
follows-only filtered view remains one keystroke away for a user who wants to
narrow it. Within new scan, a dedicated "replies to you" pass (described
above) runs across every board regardless of follow state, since a reply is
always worth surfacing.

Selecting an item from new scan jumps directly into that resource
pre-positioned at the first unread item — mechanically, calling the
resource's own existing keyset-pagination entry point with `after=` set to
the user's stored cursor, not a new navigation primitive.

#### Local search

Implemented (issue #56's last piece). Local search is a new, separate
capability from the item picker's simple, per-call substring name match
(`pick_item`'s own search command, unrelated and unchanged — see below):

- **scope**: only this node's own already-stored content — approved board
  posts (subject/body), approved file entries (filename/description), and
  retained channel scrollback (message body). Never content this node does
  not itself carry — there is no Link-wide query protocol, and this design
  does not imply or require one;
- **mechanism**: SQLite FTS5 virtual tables (`post_search`, `file_search`,
  `channel_message_search`), kept in sync with `posts`/`files`/
  `channel_messages` by explicit calls from `netbbs.boards.posts`/
  `netbbs.files.entries`/`netbbs.chat.scrollback` at every write path
  (create/edit/approve/delete/expire/trim) — deliberately not SQL
  triggers, matching this schema's existing convention of zero triggers
  anywhere else and keeping the sync logic visible in Python. `post_search`
  holds only the *resolved current* approved revision of a post's edit
  chain (mirroring `_resolve_current_version`'s own "newest approved row
  for this root" query) — a superseded revision, a still-pending edit, or
  a root with no approved revision left is never indexed.
  `channel_message_search` is pruned in the same statement that trims
  scrollback's own ring buffer, so a search can never surface a message
  already gone from retained scrollback.
  FTS5 availability was traced, not just assumed, for this project's actual
  NetBSD/pkgsrc target: `lang/python312`'s Makefile buildlinks against
  `databases/sqlite3` (not an amalgamation bundled into Python itself), and
  that package's own Makefile passes `--fts5` unconditionally in
  `CONFIGURE_ARGS` — so pkgsrc's Python `sqlite3` module should always have
  it. A build lacking it fails the schema migration loudly
  (`sqlite3.OperationalError: no such module: fts5`) rather than degrading
  silently, consistent with this project's "fail clearly" convention;
- **authorization**: a search result set passes through the exact same
  visibility rules (level/age/Community gates for boards and file areas,
  `netbbs.net.chat_flow.list_visible_channels_for` for channels) normal
  browsing already enforces — search can never be a side-channel that
  reveals a restricted resource's existence or content;
- **privacy, explicit**: a user's search query text is never transmitted to
  any peer or broadcast over Link, by default and without exception in this
  design. Searching a Linked board only ever searches this node's own
  locally carried copy of it. A future Link-wide search capability, if ever
  built, is a distinct protocol extension requiring its own explicit design
  (rate limits, query exposure, opt-in) — never an implied consequence of
  local search existing;
- **UI**: a new, always-shown `[F]ind` main-menu entry (`netbbs.net.
  scan_and_find._find_screen`), alongside `[N]ew scan` — prompts for one
  free-text query, matches it against all three content types at once, and
  jumps straight to a selected hit: a post/file lands on the exact matched
  item (`netbbs.search.post_jump_cursor`/`file_jump_cursor` compute the
  `after=` cursor that makes it the first item shown, reusing the same
  `initial_cursor` parameter `[N]ew scan` already threads through
  board/file-area viewing) rather than just opening its board/area at the
  default newest page. A channel message instead just enters its channel —
  channels have no "jump to one message" concept, the same limitation
  `[N]ew scan`'s own channel dispatch already accepts.

Local, in-page substring matching over a short list (`pick_item`'s own
search command) is unrelated and unchanged — it is not "search" in this
section's sense, just incremental filtering of an already-open, already
access-checked list.

**Integrity checking and rebuild (issue #74).** Because the three FTS
tables above are synced by explicit per-write-path calls rather than one
shared transaction with the authoritative write, a crash between the two,
a future write path that forgets to call the right reindex function, or a
restored older backup can leave them stale with no prior way to detect or
repair it. `netbbs.search.check_index_integrity(db)` reports drift
(missing/stale/extra entries, by id only — never the drifted content
itself) for all three tables against authoritative `posts`/`files`/
`channel_messages` data; `netbbs.search.rebuild_indexes(db)` replaces
their contents outright, using the exact same "what should be indexed"
computation the check compares against, so a rebuild always converges to
a clean check immediately after. Exposed as a standalone maintenance
command, `python -m netbbs.search check|rebuild --db PATH`, mirroring
`python -m netbbs.backup`'s own subcommand shape.

**Explicit decision: startup detects nothing automatically.** Unlike
`Database.check_integrity`'s `PRAGMA integrity_check` (a full-database
scan run once at every node startup, §13's startup/crash-recovery
rules), FTS drift checking is *not* wired into node startup. The
database-corruption check is cheap relative to node startup and guards
against a failure mode (disk-level corruption) that can occur at any
time regardless of how careful this codebase's own write paths are; FTS
drift is a narrower, rarer failure (a missed reindex call, a crash in
one specific window) whose check cost scales with indexed content
rather than staying close to constant. Treat it as an operator-run
maintenance action for now — after a crash, an interrupted migration,
or a restored backup — rather than a mandatory gate on every start.
Wiring a summary into the `[D]iagnostic log`/SysOp status surface is
possible future follow-up, not required by this decision.

### 6.7 Self-update

The updater uses explicit GitHub Releases over HTTPS rather than arbitrary
branch HEAD. Current foundations include version comparison, release checking,
safe archive extraction, persisted state, and database snapshot/restore
primitives. Scheduled release checking exists; complete apply/re-exec and
rollback orchestration still requires operational validation.

Before an update which may migrate the schema, snapshot the database so binary
and schema can roll back together.

The intended apply model is:

- check at startup, manually, and on a daily schedule;
- drain live sessions before a live-node restart;
- replace the on-disk release and re-exec the process;
- retain the previous release and restore it, together with the database
  snapshot, after failed startup.

HTTPS and GitHub are currently the update trust boundary. Additional release
signing is not required by the present design, though it remains a possible
hardening step.

GitHub Releases and tagged source are the only official NetBBS distribution
and update channel. External package managers may supply dependencies, but a
pkgsrc, apt, or other independently managed NetBBS package is not supported:
it would duplicate ownership of installed application files and compete with
the GitHub-based updater.

The automatic check/apply policy must have an operator-visible off switch.

---

## 7. NetBBS Link: identity, events, and compatibility

### 7.1 Phase-3 safety boundary

Phase 3 is for private, controlled federation. It may use local blocklists as an
interim abuse control, but it is not safe to expose broadly to unknown peers
until Phase 4 defines and implements trust, probation, reputation, and
quarantine.

### 7.2 Canonical event envelope

A durable Link event uses a signed envelope:

```json
{
  "netbbs_protocol": 1,
  "object_type": "...",
  "payload": { ... }
}
```

The content ID and signature cover the entire canonical envelope, including the
object type. Object type is therefore **mandatory domain separation**: it is
intrinsic to the exact bytes that get hashed and signed, not a caller
convention a future event type could accidentally bypass by reusing another
type's shape.

**Canonicalization rule** (binding and language-independent — issue #11):

- Compact JSON: no insignificant whitespace, `":"`/`","` separators only.
- Object keys sorted by exact Unicode codepoint sequence, at every nesting
  depth, after normalization (below).
- Every string is recursively normalized to Unicode NFC before serialization —
  object member names as well as values, at every nesting depth, not values
  alone. Two payloads differing only in normalization form (precomposed versus
  combining-mark sequences) canonicalize identically and share one content ID,
  whether the difference is in a value or in a key. Two distinct source keys
  that would normalize to the same string are a normalization collision and
  are rejected outright, the same way a duplicate wire key is (below) — never
  silently resolved by whichever one happens to overwrite the other.
- Floating-point values are forbidden anywhere in a hashed or signed field:
  float serialization is not reliably deterministic across languages and
  platforms.
- Any other JSON number is an integer and must fall within
  `[-(2^53 - 1), 2^53 - 1]` — the widest range exactly representable as an
  IEEE-754 double, matching JavaScript's/JSON's own safe-integer bound. No
  current field approaches this bound; the rule exists so a future field
  cannot silently produce bytes only an arbitrary-precision-integer language
  can hash consistently.
- `true`/`false` are booleans, never conflated with the integers `1`/`0`.
- A field that does not apply to a given event omits the key entirely.
  Storing it as an explicit JSON `null` is a **different, distinct canonical
  value** — `{"parent_post_id": null}` and `{}` must never share a content ID.
  Each event schema states, field by field, which behavior applies; a builder
  must not choose between omission and `null` ad hoc.
- Wire JSON containing the same key twice in one object, at any nesting
  depth, is rejected outright before it is canonicalized, hashed, or
  verified — never silently resolved by a "last one wins" rule. Two
  different JSON parser implementations can disagree about which duplicate
  value wins; a sender and receiver that disagree would each reconstruct a
  different object from what they would both call "the same bytes."

`netbbs.boards.content_id.canonical_json_bytes` is the sole canonicalization
implementation this codebase uses to produce these bytes. Anything that
signs, verifies, or content-addresses a Link event reuses it directly, never
a second, independently-maintained implementation that could quietly drift.
`netbbs.link.events.strict_json_loads` is the reference implementation of the
duplicate-key rule, applied to every message this node's transport reads off
the wire before that JSON becomes a candidate envelope.

Golden test vectors (`tests/fixtures/link_canonical_vectors.json`, checked by
`tests/test_link_canonical_vectors.py`) pin exact canonical bytes and content
IDs for representative payloads, including Unicode normalization,
omitted-versus-null, and integer-boundary cases. An independent,
non-Python implementation of this format is compatible with NetBBS Link if
and only if it reproduces every vector's canonical bytes exactly.

Existing Python behavior implements the rule above; it is not a separate,
looser specification of its own.

### 7.3 Author references

An event author is a tagged union:

- `node_vouched_user`;
- `user_key`;
- `node`.

The verifier resolves the appropriate current signing key and, for node-owned
keys, validates its transition history back to the root identity.

Only the author tiers implemented for a specific event type are accepted. The
existence of the tagged union does not imply every tier already works for every
feature.

A `node_vouched_user` author (or a `link_message` sender/recipient) is
identified by the pair `(home_node_fingerprint, local_user_id)`, never by
`local_user_id` alone — a username is unique only within its own node, so the
pair, not the bare name, is the globally-scoped identity issue #11 asks for,
matching the `user@node-fingerprint` addressing form already used elsewhere.
`local_user_id` is the account's canonical, immutable, stored-case username
(§5); it participates in canonical bytes exactly as stored, after the same
NFC normalization every string field receives — never case-folded the way
local login/uniqueness lookups are, since a signed event fixes one exact
string forever, not a case-insensitive equivalence class.

### 7.4 Immutable content and state-changing chains

There are two event classes.

#### Immutable creation events

Examples include a board post or file descriptor. Their content ID identifies
the complete immutable object. Nodes may differ only in whether they possess or
locally suppress it.

A random nonce distinguishes two intentional posting actions with otherwise
identical visible content.

#### Per-object state chains

Edits, metadata changes, grants/revocations, key transitions, origin transfer,
closure, and membership changes extend a per-object chain. Each new event
references the state/event it extends.

Effective state is the projection/fold of the valid chain. An incoming event is:

- a valid extension of the current state;
- an already integrated ancestor, therefore an idempotent no-op;
- a genuine competing extension/fork requiring the object’s defined policy.

Transport deduplication is only a performance optimization. Permanent replay
safety comes from the authoritative object state or chain, not from a purgeable
“seen ID” cache.

Tombstones are chain events, not deletion of history. Local byte pruning cannot
resurrect state if the permanent projection rules remain intact.

Two events both validly extending the same predecessor at the same instant is
impossible by definition: a chain has exactly one current head, and an
incoming event either extends it (accepted) or does not (rejected as
reordering, or handled as a fork, per the object's own policy). `created_at`
is descriptive metadata for display and audit, never the mechanism that
orders or authorizes a chain extension — two genuinely successive events can
legitimately share one clock's timestamp resolution. Reconstructing a chain
from storage (for example, after a restart) must walk the same
`previous_event_id`/head-pointer links original acceptance already verified,
or rely on the storage layer's own locally-assigned, monotonic receipt
ordering — never re-sort on the payload's own claimed `created_at` alone.
Ordering among unrelated immutable events for local presentation (for
example, a board's post listing) is a separate, local concern with its own
stable tie-break, not a protocol question.

### 7.5 Version and unknown-event behavior

`netbbs_protocol` changes only for incompatible wire semantics. Additive event
types or optional fields need not force a protocol bump when old peers can
safely preserve them.

Peers exchange supported protocol information during authenticated contact.
Unknown event types or unsupported versions may be stored and relayed opaquely,
but must not be projected, displayed, or treated as authority by a node which
cannot interpret them.

Unknown fields within a known signed event must be preserved in the original
signed representation. A node must not strip and reserialize them in a way
which changes the signed bytes.

---

## 8. NetBBS Link transport, discovery, and distribution

### 8.1 Traffic-family split

Asynchronous/store-and-forward features use signed HTTP+JSON:

- key and endpoint state;
- boards and Link messages;
- future file catalogues and chunk requests;
- governance events.

Real-time Link chat will use a persistent mutually authenticated Noise channel
with the node transport key. Do not force asynchronous and real-time traffic
through one protocol merely for uniformity.

### 8.2 Hello and endpoint state

A hello is self-authenticating and carries enough root and transition state to
resolve the current signing key, plus a signed endpoint descriptor.

Endpoint descriptors may advertise ordered addresses and relay information.
The newest valid descriptor wins; stale repeats are harmless.

The protocol logic remains transport-independent. The `aiohttp` adapter is the
boundary translating protocol messages to real HTTP requests and responses.

### 8.3 Bootstrap and peer discovery

Bootstrap sources are combined, not exclusive:

1. operator-configured seeds;
2. the software-shipped reliable-nodes fallback (Reliable Link first; §16,
   issue #219);
3. the live reliable-nodes roster, fetched daily from
   `https://www.netbbs.org/reliable-nodes.json` and preferred over the
   fallback once any fetch has succeeded -- one list serving default seeds,
   asynchronous relay candidates, and the live-relay anchors (§8.10.3), and
   dialed only after the SysOp accepts reliable-node participation;
4. signed/verified peer-list exchange after contact;
5. bounded fallback attempts to discovered candidates when normal seeds fail.

Seed or peer introduction never implies trust. Identity verification is
cryptographic and independent of the network address which introduced a peer.

A compromised bootstrap source can attempt an eclipse or steer connection
attempts, but cannot impersonate an existing node without its key.

### 8.4 Full and outgoing-only nodes

A full peer advertises reachable addresses and accepts inbound Link traffic.
An outgoing-only node initiates connections but cannot be dialed directly.

Multiple addresses are tried in order. Simultaneous HTTP dials require no
connection-role tiebreak because they are independent idempotent request/
response exchanges, not competing persistent sessions.

### 8.5 Relay service for outgoing-only nodes

Outgoing-only nodes select a small redundant set of reachable full peers based
on direct-observation reliability. Relay participation requires signed consent.
A node may opt out of serving relays and may cap the clients/resources it serves.

Accepted relays are published through endpoint state and replaced when observed
reliability degrades.

The relay mailbox currently supports opaque encrypted Link-message envelopes:

- relays see routing metadata and size, not message content;
- storage is bounded;
- pickup authenticates the intended recipient;
- the recipient re-runs normal event verification rather than trusting the
  relay’s claim;
- relaying does not introduce strangers or weaken the rule that sender and
  recipient identities must already be known sufficiently to verify and
  encrypt.

Reliability scoring is direct-observation operational data, not Phase-4 social
reputation.

### 8.6 Current synchronization model

Current background sync:

- contacts configured/cached seeds and candidates;
- performs hello/peer discovery;
- pushes the complete locally originated supported event set;
- relies on idempotent acceptance;
- sends targeted Link mail directly or through a selected relay;
- requests and applies bounded inventory/pull-based catch-up for linked
  boards, channels, and file-area catalogues from every seed dialed that
  pass. Responders include carried resources absent from the request, so
  an empty inventory can discover a first resource through a carrier
  (§8.8, issues #85/#94).

This is intentionally simple but incomplete.

Not yet present:

- efficient per-peer deltas beyond a full per-board known-ID list (fine at
  this project's declared scale; a compact digest would be needed beyond it);
- complete retained-event and dedup-purge policy — `key_transition` alone
  is purged (§8.9, issue #86, closed); every board-scoped type stays
  unbounded, stated explicitly as still-needed, not silently deferred;
- public-network backpressure and abuse handling.

### 8.7 Store-and-forward goal

The eventual model supports nodes which are offline for extended periods and
resume synchronization later. Causal relationships come from parent/chain
references; timestamps are secondary ordering data, and content IDs provide a
deterministic final tiebreak for truly concurrent siblings.

Persistent dedup uses exact IDs, not Bloom filters. False-positive data loss is
unacceptable. Retention cleanup must never turn an old state-changing event into
something re-applicable.

### 8.8 Inventory/pull-based catch-up and multi-hop relay (issue #85)

§8.6 named two concrete gaps in the push-only, direct-pairwise sync model:
no pull-based catch-up, and no multi-hop propagation (a node carrying
Alice's board events never relays them to Carol). This section specifies
both, deliberately reusing existing machinery wherever possible rather than
adding new protocol-verification surface.

**Scope.** Signed board-scoped events, linked-channel genesis/messages, and
linked file-area catalogue metadata are included. File bytes are fetched
separately and never appear in inventory. Identity (`key_transition`) events are
already gossiped to every configured seed every pass regardless (§12) and
are small enough that this has never been the gap; Link messages are
point-to-point by design (§10) and are explicitly excluded from any
multi-hop relay, matching their existing "no relay from a stranger" routing
boundary (§10.4) — nothing here changes how `link_message` is delivered.

**`InventoryRequest` — signed, destination-bound, fresh, and not a canonical
event (revised by issues #106/#124; originally shipped unsigned).** This is a bookkeeping request about
what the requester already has, not durable authored content, so it still
needs no content-addressing or gossip-replay semantics of its own — no
chain and no `content_id`. It **is** always signed by the requester's own
current operational signing key, the same "always signed by the
requester's own current key" shape §12's `relay_consent_request` already
established. Before Link v1 interoperability is frozen, issue #124 makes the
additional fields below required rather than preserving the replayable
pre-freeze request shape.

```
InventoryRequest {
  requester_fingerprint: string,
  responder_fingerprint: string,
  created_at: timestamp,
  nonce: 128-bit random hex string,
  signature: bytes,
  boards: { board_id: [known_content_id, ...], ... },
  channels: { channel_id: [known_content_id, ...], ... },
  file_areas: { area_id: [known_content_id, ...], ... }
}
```

The signature covers every field except `signature` itself. A responder
requires `responder_fingerprint` to equal its own root fingerprint, rejects
timestamps more than five minutes old or ahead of its clock, and rejects a
recently seen `(requester_fingerprint, nonce)` pair. The replay cache is
process-local and capped at 4,096 entries; timestamp freshness preserves the
bounded replay window across restart. This prevents a captured request from
being redirected to enumerate a different peer or replayed indefinitely.

`boards` is keyed by every `board_id` the requester itself currently
carries (bounded by its own `max_carried_boards` quota, §13.9 — the request
size is therefore already bounded by an existing cap, not a new one) mapped
to that board's full set of content IDs the requester already has for it.
`channels`/`file_areas` (§9.6, §11) are the identical shape for linked
channels and linked file-area catalogues respectively.

**Route: `POST {LINK_PATH_PREFIX}/inventory/{fingerprint}`**, mirroring
`/events/{fingerprint}`'s existing convention (`fingerprint` names the
requester — and, since issue #106, is now actually checked: see below).
The response is **not** a new envelope type either — it is the same raw
JSON event-list shape `push_events`'s request body already uses:

```
{ "events": [ <raw event dict>, ... ], "more_available": bool }
```

**Responder-side diff.** For each `board_id` this responder itself
currently carries — whether or not it appears as a key in the request at
all (see the discovery paragraph below) — return every board-scoped event
on file for that board whose `content_id` is not in the requester's
declared list for it (an absent key is treated as an empty declared
list). The diff unions three differently-shaped sources, not `link_events`
alone: this node's own self-originated genesis/lifecycle (`boards.
link_genesis_json`/`link_lifecycle_json`, never routed through
`handle_events` at all, so never in `link_events`), any post/edit a
*local* user authored on any Linked board regardless of whether this node
originated or merely carries it (`posts.link_event_json`, populated only
by self-authorship, per `netbbs.link.boards.queue_board_post_if_linked`'s
own scope), and every peer-received event this node has accepted
(`link_events`, filtered by the new `board_id` column — see the schema
change below). A `board_id` the responder does not itself carry is
silently skipped, never an error — "not carrying this board" is already a
legitimate, honestly-represented answer (§9.3). **This is the entire
multi-hop mechanism**: a node that only *carries* board X (never
originated it) can now answer an inventory request for X from a third
node, because the diff draws from everything this node has on file for
that board, not only what it originated.

**Empty-request discovery (issue #94) and its authentication precondition
(issue #106).** The diff above answers for every `board_id`/`channel_id`/
`area_id` the *responder* carries, not only ones present as keys in the
request — so a requester with nothing carried yet can send an entirely
empty `InventoryRequest` and still discover its first Linked board/
channel/file area, rather than being stuck needing to already know an ID
it has no way to learn. Before this discovery behavior existed, the lack
of any authentication on this route cost nothing: an arbitrary caller
still had to already know a specific ID to ask about. Once an empty
request could return *everything* a node carries, the same unauthenticated
route became a resource-enumeration/content-disclosure endpoint for
anyone on the network, not just configured/verified Link peers. `LinkNode.
handle_inventory_request` therefore requires, before any diff logic runs:
`fingerprint` (the URL path segment) must already be a completed peer (the
same "no pull from a stranger" boundary `handle_events`/`handle_peer_list`
already enforce); the signed `requester_fingerprint` inside the request
must equal that same `fingerprint` (a completed peer cannot enumerate on
some *other* peer's behalf); and `signature` must verify against that
peer's current resolved signing key — proving current possession of the
identity, not merely a previously-observed, publicly-discoverable
fingerprint (fingerprints are exactly that: discoverable via the
deliberately-unauthenticated `/peers` route, §8.3). The signed responder,
freshness, and nonce checks above then prevent cross-responder reuse and
replay. The governing
invariant: a completed, cryptographically verified peer may send an empty
inventory and discover everything this node carries; an arbitrary
unauthenticated HTTP client may not enumerate anything. A request failing
any of these checks is refused outright (HTTP 403) — there is no
degraded/partial-answer tier.

**`handle_events` itself needs one correctness fix to make this
verifiable, not zero changes.** Every board-scoped branch (`board_genesis`,
`board_post`, `board_post_edit`, `board_origin_transfer_offer`/
`_accepted`) previously required the wire-level `sender_fingerprint` to
*equal* the content's own claimed origin/author, resolving the signing key
to verify against from `self.peers[sender_fingerprint]` — correct for
direct delivery, but structurally incompatible with relay: a genuinely
relayed event's wire sender (the carrier) is a different node than its
signed author/origin, so requiring equality made multi-hop content
unconditionally unverifiable, not merely unsupported. The fix resolves each
branch's signing key against the **content's own claimed origin/author
fingerprint** (already present in its payload) instead of the wire sender —
but only if that origin/author fingerprint is *itself* already a peer this
node has independently completed a hello with (`self.peers.get(...)`,
raising the same `LinkProtocolError` "no relay from a stranger" shape
otherwise). This preserves the exact same safety property in spirit —
nothing is ever accepted whose signing key this node can't independently
verify via its own previously-established trust — while correctly relocating
*which* fingerprint that trust check applies to: the content's author, not
whoever happened to relay the bytes. The wire-level `sender_fingerprint`
must still itself be a completed peer (unchanged, checked at the top of
`handle_events` as before) — relay only ever happens between two nodes each
independently already known, never introducing a genuine stranger on
either end. `key_transition` and the `link_message` family are explicitly
untouched — messages remain point-to-point by design (§10) and were never
part of this issue's scope.

**Applying the response needs no *new* acceptance path beyond that fix.**
The requester feeds the returned `events` list through the now-corrected
`LinkNode.handle_events` exactly as it already does for a push response —
chain/dedup logic and materialization are otherwise unchanged. The new
implementation surface is (a) the `handle_events` fix above, (b) the
responder's three-source diff query, and (c) a client-side loop that issues
the request and applies the response.

**A real, worth-stating limitation this implies:** a receiving node can
only accept relayed content whose author/origin it has *at some point*
directly completed a hello with — multi-hop propagates *content* through
an intermediary, but does not substitute for a receiving node's own
independent identity verification of who ultimately signed it. In practice
this is rarely restrictive at this project's declared scale (§14): seed
configuration plus peer-list-driven candidate fallback (§8.3) already tend
to bring most nodes in a small-to-medium deployment into direct contact
with each other over time. A node that has truly never verified a given
origin's identity by any means still cannot accept that origin's content
via a relay, exactly as it already could not accept it directly — this
issue does not weaken that boundary, only lets it be satisfied through a
past hello rather than requiring the origin to be *currently* reachable.

**Empty inventory is discovery, not "ask about nothing" (issue #94).**
Although each request dictionary lists what the requester currently
carries, the responder walks the union of requested and locally carried
IDs. A missing key therefore means "the requester has never seen this,"
and the responder returns that resource's events subject to the same
authentication, origin-verification, quota, and response-size boundaries.
This lets a node discover its first board/channel/file-area catalogue
through a carrier without weakening the independent-known-origin rule
above.

**Bounded response size.** Capped at the existing `_MAX_EVENTS_PER_REQUEST`
(200, §13.9) — the same constant `handle_events` already enforces on the
receiving end, not a new number. If more than that many events are missing
for the requested boards, `more_available` is `true` and the requester
simply asks again next pass; because its own `known_content_id` list for
each board grows after every partial response, each subsequent pass
naturally asks for a shrinking remainder — no separate pagination cursor is
needed.

**Requester side (`netbbs.link.sync`).** Each pass, after the existing
per-seed push loop (§12) completes for a given seed, that same seed also
receives one `InventoryRequest` covering every board this node carries.
Not sent to one arbitrary "best" peer — every seed already dialed that pass
gets asked, since not every peer necessarily carries every board this node
does, and the push loop already iterates all of them regardless. A seed
that carries none of the requested boards simply returns an empty event
list; this is indistinguishable from (and no more expensive than) today's
existing per-seed push tolerance for an uncooperative peer.

**No loop or amplification guard is needed beyond what already exists.**
This is pull-based and diff-first by construction: nothing is transmitted
unless a requester explicitly asks for a board it has already decided to
carry, and the diff is always relative to what the requester already
reports having. A fully-connected mesh does not flood — it converges,
because every node's own request naturally shrinks once it has caught up,
and dedup (`known_event_ids`) makes any redundant delivery a no-op
regardless.

**Schema change.** `link_events` gains a nullable `board_id` column,
populated for the five object types above (read directly from each one's
own `payload["board_id"]`) and left `NULL` for every other object type.
Backfilled for existing rows via `json_extract` against the stored
envelope, never requiring re-verification of already-accepted events. A
covering index on `(board_id, object_type)` keeps the diff query cheap as
`link_events` grows — this is exactly the kind of query the table did not
need to serve before this issue, since nothing previously asked "everything
for board X" rather than "everything from sender Y."

**Deliberately not addressed here** — sending a requester's complete
per-board content-ID list every pass does not scale indefinitely for a
board with a very large post history; a compact digest (Merkle-tree-style
or otherwise) would reduce request size for that case. Not worth building
at this project's declared scale (§2.3: dozens-to-low-hundreds of
concurrent sessions, small-to-medium Link deployments) — the same
"exact IDs, not Bloom filters" simplicity §8.7 already chooses for local
dedup storage applies here to the wire exchange too. Revisit only if a real
deployment shows this cost is actually a problem, not preemptively.

**Explicitly deferred to issue #86, not part of this issue.** No retention
or purging of `link_events` changes as part of this work — every event
handled here is durable, unbounded-lifetime state exactly as it already is
today. Issue #86's retention/purge policy must be designed with this
issue's shape in mind (a purged event a slow-to-reconnect peer still needs
for catch-up must never be silently unavailable, or "eventually converges"
above would stop being true) — that is precisely why #86 is sequenced
after this issue, not the other way around.

### 8.9 Event/dedup retention (issue #86)

**Part 1: the chain-idempotency gap `netbbs.link.store` named.** Every
board-scoped `handle_events` branch self-heals an exact resend against its
own *authoritative* state (never against `known_event_ids` alone) — except
`board_origin_transfer_offer`/`_accepted`, which previously depended
entirely on the fast dedup cache still holding the content_id. A resend of
a still-pending offer, or an already-accepted transfer, after a
hypothetical cache purge would have been misread as a genuine conflict
("already has an outstanding offer" / "no outstanding offer on file") and
rejected — not a security hole (nothing is ever mis-applied), but exactly
the gap blocking any purge policy from being provably safe. Fixed the same
way `key_transition`/`board_post_edit` already do it: check whether the
incoming event's own `content_id` already matches the current pending
offer (`board_lifecycle.pending_offer`) or the current lifecycle head
(`board_lifecycle_head`) *before* treating a second sighting as a
conflict, self-healing `known_event_ids` from that authoritative state
rather than erroring.

**Part 2: what can actually be purged.** Before choosing a retention
window, each object type was traced for what *else* depends on its
`link_events` row surviving — restart reconstruction (`load_link_node`)
and, since issue #85, this node's own ability to answer an inventory
request for it:

| Object type | Durable elsewhere? | Purgeable in this issue? |
|---|---|---|
| `key_transition` | Yes — `link_peers.transitions_json` is the authoritative source `load_link_node` reconstructs `sender.transitions` from; the `link_events` row exists only to fast-path a resend and was never itself load-bearing. | **Yes.** |
| `board_genesis` | Yes — `boards.link_genesis_json` durably holds it for both self-originated and carried boards, read unconditionally by both `load_link_node` and §8.8's own `_all_board_events`. | Not in this cut (see below). |
| `board_post` | **No**, for a peer-received post — `posts.link_event_json` is only ever populated for a *locally-authored* post (`queue_board_post_if_linked`), never for a materialized/carried one. Its `link_events` row is the *only* record `board_post_edit`'s own `self.events.get(root_post_id)` acceptance check and §8.8's inventory diff can draw on. | No. |
| `board_post_edit` | Same gap as `board_post` for a peer-received edit — `post_edits[root_post_id]` reconstruction and inventory serving both depend on the row. | No. |
| `board_origin_transfer_offer`/`_accepted` | **No** — `board_lifecycle_head`/`pending_origin_transfers`/`board_origin` are reconstructed *entirely* from `link_events` rows for a peer-received transfer; nothing else durably records "what the current lifecycle head is." | No. |
| `link_message` family | Not traced in this issue — deferred with the rest of this row. | No. |

**Policy actually implemented: a bounded, age-based purge for
`key_transition` only.** `netbbs.link.store.purge_expired_key_transitions`
deletes `link_events` rows where `object_type = 'key_transition'` and
`received_at` is older than a fixed retention window (90 days — a plain
module constant, not a new SysOp-configurable `LinkConfig` field, matching
this project's own restraint principle: a low-volume event type doesn't
need a dedicated tunable yet). Called inline on every accepted
`key_transition`, the same "purge on write, scoped to the same table this
write just touched" shape `LinkDiagnosticLogHandler.emit` already
established for `link_diagnostic_log` — not a separate scheduled task.

**Everything else stays unbounded in this issue, explicitly, not
silently.** `board_genesis` turned out to already be redundant with
`boards.link_genesis_json` and could plausibly be purged too, but is left
alone here to keep the rule simple (nothing board-scoped is purged this
round) rather than special-casing one board-family type while the other
four remain load-bearing. Purging `board_post`/`board_post_edit` safely
would need a real answer to "has every peer that might still need this via
inventory already caught up" — a harder question than this issue's own
scope, and a legitimate follow-up if `link_events` growth from board
content specifically ever becomes an operational problem in practice
(§13.6's `[L]ink status` already gives a SysOp visibility into growth via
the database file size, per the existing diagnostic-log-growth precedent).

### 8.10 Real-time Noise sessions (issue #148)

Real-time Link traffic uses a persistent TCP connection protected by
`Noise_XX_25519_ChaChaPoly_BLAKE2s`. XX is required because either side may
first encounter the other's current operational key during the connection;
neither side assumes the remote static key is preconfigured. This is a
separate traffic family from signed HTTP+JSON. A live chat frame is not a
canonical Link event and never enters inventory, relay mailboxes, event
retention, or asynchronous retry queues.

The Noise static X25519 key is derived from the existing Ed25519 operational
transport key using PyNaCl/libsodium's supported Ed25519-to-Curve25519
conversion. NetBBS does not create a fourth long-lived node key or introduce a
second transport-key rotation mechanism. During the encrypted XX handshake
each side sends a versioned identity payload containing its stable root
fingerprint, root public key, and root-signed transport transition chain. The
receiver:

1. verifies the root fingerprint and transition chain;
2. resolves the current authorized Ed25519 transport key;
3. converts that public key to X25519;
4. requires it to equal the Noise static key authenticated by the handshake;
5. applies the local Phase-4 node transport decision before accepting any
   application frame.

A stale, revoked, forked, malformed, or differently bound key fails the
connection. The remote node label, endpoint, DNS name, and TCP address are
never identity authority. Transport-key rotation ends sessions using the old
key; reconnect performs a fresh handshake against the new verified chain.

The endpoint descriptor advertises real-time TCP addresses separately from
HTTP addresses. An outgoing-only node may dial a reachable full node. Two
nodes which both cannot accept inbound connections meet through a live relay
(§8.10.3, issue #168) -- a raw-socket proxy below the Noise layer, a separate
mechanism from the asynchronous relay mailbox, never tunneled through it.
Asynchronous linked-channel events continue to work regardless.

#### 8.10.1 Session framing and ownership

Handshake and transport records use an unsigned two-byte big-endian length
prefix followed by exactly one Noise message. Zero-length records are invalid.
The Noise limit of 65,535 bytes is an absolute ciphertext ceiling; NetBBS sets
a lower application plaintext limit of 16 KiB. Decrypted application payloads
are strict UTF-8 JSON objects: duplicate keys, floats, unsafe integers,
unknown protocol versions, missing required fields, and trailing data are
rejected. Unknown message types produce a bounded protocol error and do not
gain side effects.

Every application object contains `version`, `type`, and a session-local
`message_id`. IDs are bounded strings and deduplicated within a bounded
per-session replay window. The first implementation supports:

- `subscribe` and `unsubscribe`;
- `presence_snapshot` and `presence_delta` (channel-scoped);
- `node_presence_snapshot` and `node_presence_delta` (issue #164, node-wide
  -- see §8.10.2);
- `channel_message`;
- `scrollback_snapshot` (issue #194);
- `relay_request`, `relay_waiting`, `relay_ready`, `relay_reject` (issue
  #168, §8.10.3 -- the live-relay rendezvous);
- `direct_message` (issue #168, §8.10.3);
- `ping`, `pong`, `error`, and `close`.

One node owns at most one live session per remote fingerprint. If simultaneous
inbound and outbound connections exist, the lower fingerprint keeps its
outbound connection and the higher fingerprint keeps its inbound connection;
the rule is applied only while both candidates exist, so a sole usable
connection is never discarded. Reader, writer, heartbeat, and reconnect tasks
are owned by one session/supervisor object. Its close path cancels and gathers
every task without masking the initiating failure.

Outbound frames use a bounded queue and never let one slow peer block another.
Subscriptions, remote presence entries, message rate, protocol strikes, and
concurrent handshakes are bounded per peer and node. A full queue drops the
session with an explicit slow-consumer reason rather than silently losing a
state transition. Heartbeat leases expire silent peers; reconnect uses bounded
exponential backoff with jitter and resets only after a stable authenticated
session.

#### 8.10.2 First live linked-channel vertical

A subscription names an already carried `channel_id`. The receiving node
checks that the channel exists, is linked, is locally allowed by trust policy,
and is available to the subscribing peer. Authorization is checked again for
every received message; a successful subscription is not a permanent grant.

Live channel messages are ephemeral node-attested assertions. They carry the
canonical local user ID and display label at the authenticated sending node,
the channel ID, body, creation time, and session message ID. They are not
individually signed canonical events: the authenticated Noise session
attributes them to the sending node, and the UI renders the human identity as
`user@node`. Password-only users can therefore participate without acquiring
a personal signing key. The sending node remains responsible for enforcing its
local membership, mute, and moderation rules before transmission; the
receiving node independently enforces its own node/user/content policy before
display.

Presence is leased, scoped to subscribed linked channels, and advisory. A
snapshot establishes current state after subscription; deltas update it.
Disconnect or lease expiry removes that node's remote presence without
persisting synthetic leave events.

**Node-wide presence (issue #164)** is a second, independent presence
concept, not a generalization of the channel-scoped one above: it answers
"who's online on this node, right now" -- the same question the local Who's
Online screen already answers -- across every node a live session currently
exists with, not gated on shared channel membership. A node broadcasts a
`node_presence_snapshot` (the local online roster) the moment it starts
tracking a peer's session, then `node_presence_delta` on each subsequent
local login/logout (the account's *first* concurrent session and *last*
remaining one only -- multi-session accounts don't flap). No new trust check
was needed: establishing a live session at all already requires `ESTABLISHED`
transport trust (§12), so node-wide presence inherits that gate for free.
Caller-facing: `[W]ho's online` mixes in every currently-known remote entry
alongside local sessions: since Link-wide live private chat doesn't exist yet
(§8.10 above), selecting a remote entry states that plainly rather than
silently failing or offering an action that doesn't work.

A freshly-subscribing peer also receives a bounded, ephemeral
`scrollback_snapshot` of the origin's own recent local scrollback,
rendered once and never durably stored on the subscribing side (§16,
issue #194) — a shrunk window before the existing async catch-up path
below fills in what a live-only subscribe would otherwise miss, not a
new durability promise. The first vertical still does not offer multiple
background channel subscriptions per caller (issue #159, closed — decided
against, not a gap); live private messages and relayed sessions arrived
with §8.10.3 (issue #168). A disconnect does not queue or replay live
frames. Callers see `connecting`, `live`, and `offline/degraded` state
plus an honest notice that live traffic may have been missed;
asynchronous signed linked-channel events remain the durable catch-up
mechanism until a later decision changes that product model.

That asynchronous catch-up path (issue #164) now enforces the identical
author-trust-state visibility linked board posts already do: a linked
channel's scrollback silently omits a message whose signed author's home
node is currently `BLOCKED`/`QUARANTINED`, keyed on the event the message
carries (`link_content_visible`), never on which node happened to relay it.
`PROBATIONARY` and local messages are unaffected -- boards and channels now
share one visibility policy instead of two independently-decided ones.

#### 8.10.3 Relayed sessions and live direct messages (issue #168)

**Live relay.** A relay is any full peer with relay serving enabled
(`[link] relay_serving_enabled`, the same switch as the asynchronous
mailbox) that both parties currently hold an ordinary authenticated
real-time session with; the reliable-nodes roster (§8.3, §16 issue #219
Decision 4) is how an outgoing-only node knows which relays to stand by
at -- it keeps a reconnecting session to every reliable node it knows
while participation is accepted, since by definition nobody can dial it
first. The relay is a raw-socket proxy below Noise (§16 issue #168
Decision 1): it never holds key material, sees only ciphertext, and the
two parties run the unchanged Noise XX mutual handshake with *each other*
through it.

Rendezvous rides over the parties' existing sessions to the relay:
`relay_request {target_fingerprint, requester_fingerprint}` from the
requester; `relay_waiting` back while the target is asked; the same
`relay_request` shape forwarded to the target as an invitation (its own
fingerprint as target); the target's agreement (a `relay_request` naming
the requester) or `relay_reject {reason: declined}`; then `relay_ready
{bridge_id, peer_fingerprint, role, attach_token, attach_address,
attach_port}` to both. Each party opens a fresh TCP connection to the
attach address, sends one plaintext `NETBBS-BRIDGE/1 <token>` record, and
runs Noise XX in its assigned role (requester initiates). Both roles
verify the authenticated fingerprint against the one `relay_ready` named
before admitting the session -- the relay is an intermediary and could
pair anyone with anyone; this check is what stops that. The relay makes
no trust decision about the pair; each party applies its own `REALTIME`
policy to the other before agreeing and again after the handshake, as
for any direct session. Relaying carries no Phase-4 implication.

Bounds (Decision 2), all operator-adjustable under `[link]`, each breach
an explicit reject/close: `live_relay_max_concurrent_pairs` (8),
`live_relay_max_pending_rendezvous` (32) with
`live_relay_rendezvous_timeout_seconds` (30, reported back as
`relay_reject {timeout}`), `live_relay_max_bytes_per_second` per
direction per bridge (64 KiB -- a byte-rate bound, since the relay never
parses frames), and `live_relay_idle_timeout_seconds` (120, a dumb
"no bytes either way" timer the endpoints' own ping/pong keeps from
firing). One leg closing tears down the other. Reject reasons are a
closed set: `not_serving`, `invalid_target`, `target_unreachable`,
`at_capacity`, `pending_full`, `declined`, `timeout`, `attach_failed`,
`policy_refused` (the requester's own standing session no longer passes
the relay's `REALTIME` policy; a *target* that no longer passes is
reported to the requester only as `target_unreachable`). Every relay
answer echoes the requester's `relay_request` message id as `request_id`;
an invitation's message id is echoed by the target's agreement or
decline; a forwarding relay's upstream request id is echoed on the
upstream's answers -- so no answer to an earlier, expired attempt can
ever settle a fresh one for the same pair.

These frames made the real-time application protocol **version 3**; requiring
the invitation id on party agreements and declines makes it **version 4**.
A version-3 relay rejects that new field, just as a version-2 peer cannot
accept the version-3 frame types, so mixed versions fail once at the
authenticated handshake with the caller-visible upgrade notice rather than
timing out a rendezvous or dropping a shared channel/relay-anchor session.
A `relay_reject` carries `origin` (`relay` or `party`) so a node
that is both a relay and a party can never misroute one. Every relay
frame a party honours is correlated: a `relay_ready` or `relay_reject`
counts only from the relay this node asked, for the target it asked
about, or from the relay whose invitation it accepted, within the
rendezvous timeout -- an authenticated peer can never make a node open an
outbound connection to an address of its choosing.

**Live direct messages.** `direct_message {to_user_id, from_user_id,
from_display_label, body, created_at}` is one private line between a
user on the sending node and a user on the receiving node, over whichever
session exists or can be established: the registry's existing session,
a direct dial of the peer's advertised real-time address, or a relayed
session -- in that order. It is ephemeral and node-attested like a
channel message (§8.10.2): never stored, never a canonical event. The
receiving node re-checks the sending node's `REALTIME` policy at delivery,
then delivers exactly as a local `/msg` does (live chat sessions via the
hub, every other session via the mailbox); an unknown, opted-out, or
offline recipient is dropped silently -- the sender already checked the
peer's node-wide presence (a node that has not yet pushed its presence
gets "couldn't confirm who is online there", never a blind send), which
the peer pushes the moment the session is tracked -- and receiving a
node-presence snapshot is itself what makes the receiving side track a
session that never subscribes to a channel, so the presence is answered
and later cleared. A remote user list is not a caller's to probe.

**Anchor advertisement and chained bridges (issue #270).** A node's
signed endpoint descriptor carries an optional `live_relays` list -- the
reliable nodes it is currently standing by at (§8.3; omitted when empty,
like `relays`) -- refreshed with every hello. Session establishment then
tries, in order: the registry; a direct dial; a single-hop rendezvous at
each relay the *target* advertises (reusing a session or dialing that
relay directly, since a relay is a full peer); a single-hop rendezvous at
each of the node's own anchors; and, only for a target relay the node
could not reach itself, a chained rendezvous: `relay_request {target,
requester, via_relay}` to its own anchor R1, which reuses or dials R2 and
forwards the request with `hops: 1`. R2 runs the ordinary rendezvous with
the target, treating R1's session as the requester's side, and its
`relay_ready` to R1 names `for_fingerprint` (whom the leg is for). R1
attaches to R2 as a raw leg -- no handshake; it is a pipe -- issues the
requester its own `relay_ready`, and splices the two legs: A–R1–R2–B,
Noise still end to end, both relays seeing ciphertext only. A forwarded
request is never forwarded again (`hops` is capped at one), so a chain
is at most two relays; each relay counts its bridge against its own pair
cap and applies its own byte-rate and idle bounds, and every failure
along the chain surfaces to the requester as an explicit `relay_reject`.

Caller-facing (Decision 3): `/msg user@node-fingerprint <text>` and
`/private user@node-fingerprint` in chat, and `[M]essage` on a remote
entry in Who's online. When no path exists, whatever the reason, the
caller sees one reason-free refusal -- "can't be reached for live chat
right now" -- pointing at Link mail; nothing fails silently. Cross-node
`/dm` invites stay local-only in this vertical.

---

## 9. Linked boards and resource lifecycle

### 9.1 Promotion and genesis

An existing local board may be promoted into Link scope. Promotion creates one
signed `board_genesis` referencing the existing stable board ID; it does not
replace the board with another local object.

The node identity is the origin authority. The genesis includes descriptive
metadata and recommended defaults for carrying nodes.

### 9.2 Posts and edits

Only approved local posts are originated as `board_post` events. Password-only
users currently use the `node_vouched_user` author tier.

Self-authored edits become chained `board_post_edit` events. The original post
remains immutable. Moderator edits and tombstones require separate authorized
event types and advanced governance.

### 9.3 Carry and local materialization

A peer accepting a valid board genesis materializes a real local board copy so
users can browse carried content through the normal board UI. Carrying is more
than retaining raw protocol events — the same principle extends to a carried
board's *content*, not just the board shell itself (issue #73): an accepted
`board_post`/`board_post_edit` must become an ordinary local `posts` row, not
remain a protocol-layer record a caller-facing screen can never reach. Before
this, a carried board could verifiably receive posts while still showing
empty to every reader — `link_events` is necessary for protocol verification
and replay safety, but it is not the product database.

**Mechanism.** `netbbs.link.boards` gains `materialize_carried_post`/
`materialize_carried_post_edit`, mirroring `materialize_carried_board`'s own
shape: idempotent (keyed on the event's own `content_id`), bypassing
`netbbs.boards.posts.create_post`/`edit_post` entirely (those require a local
`User` author and mint a fresh local ID, neither of which fits received
content) in favor of a direct insert using the **event's own `content_id`
verbatim as the local `post_id`** — the same "never mint a second ID for the
same thing" precedent `materialize_carried_board` already established for
`board_id`. This has a valuable side effect: since a `board_post_edit`'s own
`root_post_id`/`previous_event_id` payload fields already name other events'
`content_id`s, and those become the corresponding local `post_id`s verbatim,
`posts.root_post_id`/`edit_of_post_id` resolve directly from the Link
payload with no separate ID-translation table.

Unlike `materialize_carried_board` (a separate `lane.run` call from the
`save_event` that persists the underlying signed event — a real, pre-existing
crash-window gap for genesis materialization, not newly introduced but not
closed here either), the new functions perform the `link_events` insert and
the `posts` projection in the same call, one transaction, one commit: a crash
between them is no longer possible for posts/edits specifically. `LinkServer.
_handle_events` calls the combined function once per accepted `board_post`/
`board_post_edit`, replacing today's separate `save_event` dispatch for those
two object types.

A reply's `parent_post_id` is set only if that parent is *already* locally
materialized — the same "no backfill, no speculative storage" rule this
project already applies everywhere gossip can arrive out of order (§8, §9.1):
an orphaned reply is materialized as a top-level post rather than blocked or
queued waiting for a parent that may never arrive.

**Author identity.** A materialized post's `author_user_id` is `NULL` — no
local account is implied or required by carrying content (issue #73's own
required test scenario) — with `author_label` synthesized as `local_user_id@
home_node_fingerprint` (the same address shape Link mail already uses) and
`author_fingerprint` left `NULL` (that column is a *local* user's own
personal keypair fingerprint, a different concept from a remote node's
fingerprint — never conflated). This requires a prerequisite fix: `Post.
author_user_id` is currently typed as a required `int` even though the
column has been nullable since the account-deletion migration (round 60's
`ON DELETE SET NULL`) — display code (`netbbs.net.login_flow`'s post-reading
screen, notably) currently calls `get_user_by_id(db, post.author_user_id)`
unconditionally. Widening the type to `int | None` and guarding every such
call site is corrected as part of this work, not deferred — a locally
deleted user's own old posts were already silently exercising this exact gap
before a remote author's posts could.

**Display and resolution.** No new resolution logic is needed:
`_resolve_current_version`'s existing `root_post_id`/`created_at DESC, id
DESC` query already picks the correct latest revision for a materialized
chain, since materialization always processes a verified edit chain in
accepted order — local `id` (strict insertion order) therefore agrees with
logical edit recency even when the remote-claimed `created_at` doesn't (clock
skew, out-of-order network delivery), the same tie-break reasoning issue #68
already established for purely local edits. `created_at` on a materialized
row is the *authored* timestamp from the signed event, never the local
arrival time — see the separate node-local-arrival-order issue (#72) for why
unread/New Scan ordering is a distinct concern from this display field.

**Local moderation stays event-history-safe by construction.** `delete_post`
only ever touches `posts`, never `link_events` — deleting a materialized
post's local row (subject to its own existing FK-blocker rules: no deleting a
post with local replies or edit-chain descendants) already cannot rewrite or
lose the signed record needed for replay safety, with no new mechanism
required. Origin recommendations (§9.1) never override this local policy,
exactly as they never override any other local access/moderation/retention
decision on a carried board.

**Idempotency, New Scan, and search.** Duplicate delivery of an
already-materialized event is a no-op (existing `post_id` found, row returned
unchanged) — no duplicate local posts or revisions. `[N]ew scan`/unread
counts (`netbbs.activity`, issue #56) need no new wiring at all: they compare
a stored cursor against `posts` rows directly, with no separate
"mark as new" call site, so any newly materialized row is automatically new
activity. Local search does need an explicit call — `netbbs.search.
reindex_post(db, board_id, root_post_id)`, the same call every other
`posts` write path already makes, right after each materialization.

**Repairing a gap.** Because persistence and projection are now atomic for
new events, the only way a `board_post`/`board_post_edit` in `link_events`
can lack a corresponding `posts` row is a node that carried boards *before*
this feature shipped. A repair pass — scan `link_events` for `board_post`/
`board_post_edit` rows with no matching `posts.post_id`, and materialize them
in chain order — closes that one-time gap and doubles as the "supported
rebuild path" issue #73's own acceptance criteria ask for, the same
"derived state must be rebuildable from authoritative data" principle issue
#74 applies to FTS indexes. Exposed as `[R]epair carried posts` in the
SysOp `[S]ystem` submenu (only shown when Link is enabled), the same
explicit-SysOp-trigger-only shape `netbbs.files.gc`'s reference-aware blob
reclaim already established — purely additive (fills in a missing row from
an already-verified signed event, never deletes or rewrites anything), so
unlike blob reclaim it needs no dry-run/confirm step.

Linked resources are carried by default within the supported topology, with a
visible local exclusion option. A local exclusion must be represented honestly
as “not carried on this node,” not indistinguishable disappearance.

Origin recommendations never override the carrying node’s local access,
moderation, retention, or legal policy.

### 9.4 Origin succession

Routine node signing-key rotation is handled by the node key-transition chain
and does not transfer resource ownership.

Voluntary board-origin transfer requires mutual consent:

1. the current origin signs an offer naming the proposed new origin;
2. the proposed origin signs acceptance;
3. peers project the new origin only after both valid events.

Only one outstanding transfer offer is meaningful at a time.

If the current origin loses all valid signing authority and cannot publish a
transfer, the board is locally recognizable as orphaned. Existing content
remains available, but no new origin-authorized state is accepted.

A fork is a new resource/genesis with a new origin and an optional
non-authoritative `forked_from` reference. Each node independently chooses
whether to carry the original, the fork, both, or neither.

Channel-side Link lifecycle will reuse these principles after linked channels
exist; there is currently no channel genesis protocol.

### 9.5 Board closure and moderator-authorized post changes (issue #88)

Three further origin-authorized event types complete the board-lifecycle and
per-post governance surfaces left open by §9.4:

**`board_closure`.** A terminal board-lifecycle event, extending the same
`board_lifecycle_head` chain §9.4's transfer offer/acceptance already extend
— signed by the board's *current* origin, referencing the chain's current
head as `previous_event_id`. Once accepted, no further lifecycle event
(another closure, or a fresh origin-transfer offer) is accepted for that
board — closure is terminal, not reversible in this slice. Closure stops new
posts (`board_post`) to the board; it does not restrict moderator edits or
tombstones of existing content, since an archived board may still need
cleanup. Materializes locally as a `boards.link_closed_at` timestamp,
enforced by `netbbs.boards.posts.create_post` the same way any other
board-level gate already is.

**`board_post_moderator_edit`.** Structurally identical to `board_post_edit`
(§9.2) — extends the same per-post `previous_event_id` chain — but signed by
the board's *current origin* instead of the edited post's own author's home
node, and carries no `author` field to cross-check against (the "self-authored
only" rule `board_post_edit` enforces on receipt simply doesn't apply to this
type). This is deliberately *not* a new cross-network moderator-grant
primitive: `netbbs.boards.posts.edit_post` already allows any local user
holding `BoardPermission.EDIT` to edit someone else's post (existing
behavior, unchanged by this issue) — that local permission check happens
once, on the origin node, before `netbbs.link.boards.queue_board_post_
moderator_edit_if_linked` ever builds and signs the event. A carrying
(non-origin) node's own local moderator action on a post it doesn't own
stays purely local and is never propagated — it has no origin authority to
assert an edit the rest of the network would recognize. Linked-board
moderator *grants and revocations* (a network-visible delegation of that
authority to non-origin nodes) remain out of scope, unchanged from this
section's prior framing.

**`board_post_tombstone`.** Also extends the per-post chain, as a terminal
entry — no further `board_post_edit`/`board_post_moderator_edit`/a second
tombstone is accepted once a post's chain head is already a tombstone. Same
origin-signed authorization model as a moderator edit; carries its own
placeholder `subject`/`body` (redaction content chosen once by
`netbbs.boards.posts.tombstone_post`, not reconstructed by convention on each
receiving node) plus an optional `reason`. Locally, a tombstone is a further
content-addressed revision — never an in-place mutation, and never
`netbbs.boards.posts.delete_post`'s hard delete, which stays reserved for a
still-`'pending'` post's rejection and refuses outright if any row still
references the target — so the edit chain, and any reply's `parent_post_id`,
stay intact. `posts.tombstoned_at` (nullable, plain `ALTER TABLE`, no
`CHECK`-widening table rebuild — `posts` is a live self-referencing FK parent,
and an earlier migration already documents why rebuilding it is
specifically unsafe) marks the terminal revision; `edit_post`/`tombstone_
post` both refuse to extend a chain whose current head is already
tombstoned. Requires `BoardPermission.DELETE`, no author bypass, matching
`delete_post`'s existing rule exactly.

All three share `board_origin_transfer_offer`'s verification shape: resolve
the board's current origin (`current_board_origin`, not the genesis's
original claim), confirm it's an independently-known peer, verify the
signature against its current signing key. None invents a new authorization
primitive — they reuse the origin's existing signing identity and, for the
two post-scoped types, an already-existing local permission check.

Still future or incomplete:

- linked-board moderator grants and revocations (delegating origin-recognized
  moderator authority to a non-origin node, rather than only the origin
  itself ever asserting a moderator edit/tombstone, as above);
- general public-network anti-entropy beyond the current bounded
  full-known-ID inventory exchange;
- Link-blanket governance surfaces and audit feeds (Phase 6).

### 9.6 Linked channels (issue #87)

Mirrors §9.1-§9.3's promotion/genesis/carry model as closely as possible
rather than inventing a parallel one, with two differences that follow
directly from how local channels already differ from local boards, not
from anything specific to Link:

**No edit chain.** Local channel messages have no edit concept at all
(chat access is participate-or-not, no read/write split, §5.4) — there is
no `channel_message_edit` mirroring `board_post_edit`, because there is
nothing locally to mirror. A `channel_message` is immutable, single-shot
content exactly like a `board_post` with no reply/parent structure (chat
scrollback is flat and chronological, never threaded).

**Origin succession is reused by reference, not reimplemented in this
issue.** §9.4's mutual-consent transfer/orphan/fork model applies
unchanged if a channel ever needs it — the same signed-offer-then-signed-
acceptance shape, the same "at most one outstanding offer" rule, the same
"orphaned means no new origin-authorized state, existing content stays"
behavior for a channel whose origin loses all signing authority. This
issue does not add `channel_origin_transfer_offer`/`_accepted` event
types — genesis/promotion/materialization/messages are the actual scope
(the "Recommended direction"'s own list), matching how governance is
explicitly deferred to Phase 6 rather than half-built here. `channels`
gains no `link_lifecycle_json` column in this issue for the same reason
— add it alongside the transfer event types themselves, when and if a
future issue actually needs it, rather than carrying an unused column now.

**Event family.** Two new object types, `channel_genesis`/
`channel_message`, structurally identical to `board_genesis`/`board_post`
minus the fields above:

- `channel_genesis`: `origin_fingerprint`, `channel_id` (the *existing*
  local content-addressed `channel_id`, never newly minted — same "promote
  an existing local resource" rule §9.1 already states for boards), `name`,
  `created_at`, and optional cascading-recommendation fields mirroring
  `Channel`'s own settable columns (`description`, `min_level`, `min_age`,
  `name_requirement`) — no `default_min_write_level`/`default_moderated`/
  `default_max_post_age_days` equivalents, since `Channel` has none of
  those to recommend a default for. Signed by the origin's current signing
  key, same as `board_genesis`. One per `channel_id`, ever — a different
  genesis for the same `channel_id` is a conflict, identical to
  `has_conflicting_genesis`'s existing rule.
- `channel_message`: `channel_id`, `author` (the same tagged union
  `board_post` uses — only `node_vouched_user` has a real build/verify
  path today, for the same reason), `body`, `created_at`, `nonce`. No
  `subject` (channel messages don't have one) and no
  `parent_post_id`/reply structure (scrollback is flat).

Canonical encoding, event-identity, and verification follow §7.2-§7.4
exactly as already specified for every other event family — no new rule
needed. `handle_events` gains two new branches following the identical
shape `board_genesis`/`board_post` already use, including issue #85's own
verify-against-claimed-origin-not-wire-sender fix from the start (no
separate "add multi-hop later" step this time — channels get it on day
one, since #85 already generalized `handle_events`'s verification model
before this issue landed).

**Carry and materialization.** A peer accepting a valid `channel_genesis`
materializes a real, locally browsable `Channel` row —
`materialize_carried_channel`, mirroring `materialize_carried_board`'s
exact shape: bypasses `netbbs.chat.channels.create_channel` (which mints a
fresh content-addressed ID from the *local* creator/timestamp, wrong for
carried content), inserts directly using the genesis's own `channel_id`
verbatim, seeds settings from the genesis's cascading recommendations, and
is idempotent (a resend, or a second peer relaying the same genesis,
returns the existing row unchanged).

A `channel_message` materializes into an ordinary `channel_messages` row
— `materialize_carried_channel_message`, using `netbbs.chat.scrollback`'s
existing insert-and-trim shape (`record_message`'s own logic, not a
parallel path), keyed by the event's own `content_id` as the row's local
identity for dedup purposes the same way a `board_post`'s `content_id`
becomes its local `post_id`. `author_label` follows the same
`local_user_id@home_node_fingerprint` synthesis `materialize_carried_post`
already uses; no local account is implied.

**A real, worth-stating consequence of reusing the existing bounded
scrollback rather than inventing unbounded storage for channel content:**
`channel_messages` is a trimmed, bounded scrollback by local design
(`netbbs.chat.scrollback`'s own configured limit, default 100), not a
permanent archive the way `posts` is. A materialized linked message is
subject to the exact same trim as a local one — old linked-channel history
ages out of scrollback precisely as old local history already does. This
is a deliberate consequence of treating a linked channel as genuinely the
same kind of resource as a local one (bounded live chat), not a silent
data-loss surprise unique to Link: the identical bound already applies
today to every channel's own local messages. A self-originated message
queued for push (`channel_messages.link_event_json`, the messages-table
counterpart to `posts.link_event_json`) that gets trimmed before any sync
pass ever pushes it is simply never propagated over Link — bounded,
honestly-scoped, matching this project's own "explicit bound, defined
behavior" principle rather than an indefinite queue.

**Idempotent duplicate delivery, restart reconstruction, and inventory
serving** all follow the identical shape §9.3 and §8.8 already establish
for boards: `channels.link_genesis_json` (new nullable column) is the
restart-safe source for a carried channel's genesis, read unconditionally
the same way `boards.link_genesis_json` already is; `channel_messages.
link_event_json` mirrors `posts.link_event_json` for self-authored
tracking. Issue #85's inventory diff extends to `channel_id`-scoped
`link_events` rows the same way it already covers `board_id`-scoped ones.

---

## 10. Link messages

### 10.1 Product model

A Link message extends the ordinary local mailbox. The user composes to a
`user@node-fingerprint` address and reads the result in the same inbox/sent UI
as local mail.

The message is point-to-point to one recipient node, not flood-filled public
content.

### 10.2 Confidentiality guarantee

The implemented confidentiality tier is `tier1_home_node_key`:

- subject and body are encrypted to the recipient node’s current signing-key
  material converted to X25519;
- network peers and relays cannot read the content;
- the recipient’s home-node operator technically can decrypt it.

This is not end-to-end encryption against the home node and must not be marketed
as such.

The X25519 key is derived from the existing Ed25519 key through libsodium’s
supported conversion. This deliberately couples encryption and signing-key
rotation and accepts the larger compromise blast radius in exchange for a much
simpler lifecycle and no separate key-distribution protocol.

Static recipient keys do not provide forward secrecy. A later compromise can
expose previously captured messages.

`tier2_personal_key` remains reserved vocabulary but is permanently out of the
planned product scope unless NetBBS gains a real client-side decryption
architecture. Server-side terminal sessions cannot render content which the
server is forbidden to decrypt, and a web-only feature is not sufficient to
justify a parallel mail system.

### 10.3 Delivery state

Transport receipt is not user delivery.

Separate signed events represent:

- accepted into the recipient mailbox;
- bounced because of unknown recipient, full mailbox, blocking, or another
  defined terminal failure;
- future expiry where retry policy requires it.

Outbound messages remain pending until an accepted or bounced event arrives.
Delivery through a relay does not change the acceptance semantics.

### 10.4 Routing limitations

Direct delivery requires a known peer with a usable endpoint. An outgoing-only
recipient may be reached through relays it has selected and published.

The current system does not introduce total strangers. The sender must already
know enough authenticated peer/key state to encrypt to the destination, and the
recipient must know enough sender state to verify the message.

### 10.5 Metadata and abuse controls

Only routing information needed by transport or relay infrastructure should be
visible outside the encrypted body. Subjects and bodies remain encrypted.

Mailboxes, relay storage, retries, and pending acknowledgements are bounded.
Blocking and quota failures must be explicit; unread data is not silently
removed to make delivery appear successful.

### 10.6 Tier-2 recipient scope (issue #90) — deferred, not permanently

**Not to be confused with `tier2_personal_key` (§10.2).** That is a
different, already-decided, permanently-out-of-scope concept: client-side
end-to-end encryption against the home node itself, blocked by a hard
architectural constraint (no client-side decryption exists). This section
is only about *recipient reachability* — delivering `tier1_home_node_key`
messages to a peer the sender has never directly, verifiably completed a
hello with — which has no equivalent hard blocker, just isn't built yet.

**What §10.4's "no total strangers" boundary actually rests on.** A
`HelloMessage` bundle is *self-authenticating* by construction (§12): a
peer that didn't hold the claimed root key's private half could not have
produced a transitions chain that both verifies against that root and
whose resolved current signing key matches the descriptor's own
signature. Verifying someone's identity has never required trusting
*who* handed you the bundle — only the bundle's own internal
cryptographic consistency. Today's requirement that this always happens
via a completed two-way hello is a stronger condition than the
verification itself actually needs.

**Issue #85 does not provide tier-2 reachability, and was never going to.**
It was tempting to assume inventory/pull-based relay already closes this
gap, since it does let a node receive board content authored by someone
it never directly synced with. It doesn't generalize to messages: #85's
own `handle_events` fix requires the content's claimed origin/author to
*already* be a peer this node has independently completed a hello with
(`self.peers.get(origin_fingerprint)`) — it relays already-authored
*content* between two ends that both already know the author, it never
bootstraps a receiving node's knowledge of a brand-new peer's identity.
Nothing in this codebase today lets a node learn a *new* peer's verified
root key/transition chain except a direct hello.

**What a real tier-2 design would require, concretely.** Because
`HelloMessage`s are self-certifying, a third party *could* safely relay
one on a peer's behalf — the sender would verify it exactly as if that
peer had dialed in directly, independent of whether the relaying node is
honest, since a tampered or fabricated bundle simply fails verification
rather than being silently trusted. The same applies in reverse for the
recipient verifying the sender. This would need: a new relayed-hello
bundle exchange (distinct from `PeerListMessage`'s own unverified
address-only exchange, §8.3 — this one must carry the *complete*
self-certifying bundle, not just an address worth trying), and encryption
proceeding once independent verification succeeds, with no other change
to §10.2's confidentiality model.

**Confidentiality and abuse implications, if built.** No new
confidentiality exposure to message *content* — encryption still targets
the real recipient's real key, independently verified, regardless of who
relayed the bundle that made verification possible. The exposure is
metadata: a relay learns that someone is asking about a specific
fingerprint, an availability/traffic-analysis concern rather than a
confidentiality break of any message body. A dishonest relay can only
withhold or refuse to relay a bundle (availability), never forge one
(self-certification), matching the same "worth trying, never blindly
trusted" property peer-list exchange already established for addresses.

**Decision: deferred, not scoped as active work.** Unlike
`tier2_personal_key`, this is not a permanent non-goal — there is no
architectural blocker, only that it is not needed to unblock or validate
current Phase 3 work, and building it now would add real new wire surface
(a relayed-hello bundle exchange) ahead of the cadence discipline §84
already states. Revisit if a real deployment need appears (e.g. issue #83's
dogfood run surfaces callers who actually want to message someone they've
never directly synced with) rather than building it speculatively now.

---

## 11. Remote file areas (issue #89)

A linked file area remains owned and stored by its source node — unlike a
linked board, where a carrying node eagerly materializes a full local copy of
every post (§9.3), file content is deliberately **not** eagerly replicated:
bytes can be large, so they are fetched on demand, in bounded resumable
chunks, only when a local user actually wants one. The catalogue (what files
exist, their names/sizes/hashes) is still gossiped and fully browsable
without ever fetching any content — mirroring §9's promotion/genesis model
for the metadata half, then adding one genuinely new mechanism (chunk
transfer) for the content half that boards/channels never needed.

### 11.1 Promotion and genesis

An existing local file area may be promoted into Link scope exactly like a
board (§9.1): one signed `file_area_genesis` referencing the existing stable
`area_id`, never a newly minted one. `file_areas` gains `link_genesis_json`/
`link_origin_fingerprint` columns mirroring `boards`' own pair exactly (the
table's pre-existing `origin_node_fingerprint` column is unrelated dead
Phase-1/2 scaffolding, left untouched, the same precedent `boards.
origin_node_fingerprint` already set — a fresh column, not a repurposed old
one). No `link_lifecycle_json`/origin-succession event types in this issue —
same deliberate deferral §9.6 already applied to channels, for the same
reason: genesis, catalogue, and transfer are the actual scope; add transfer
event types alongside a future issue that actually needs them rather than
carrying an unused column now.

`file_area_genesis` payload: `origin_fingerprint`, `area_id`, `name`,
`created_at`, and optional cascading-recommendation fields mirroring
`board_genesis`'s own shape (`description`, `default_min_read_level`,
`default_min_write_level`, `default_moderated`, `default_max_file_age_days`,
`default_min_age`, `default_name_requirement`) — one per `area_id`, ever,
same conflict rule §9.1 already states.

### 11.2 File descriptors (catalogue, no content)

`file_descriptor`: the catalogue entry for one file, gossiped the same way a
`board_post` is, but describing metadata only — `area_id`, `file_id` (the
existing local content-addressed id, computed the same way
`netbbs.files.entries.upload_file` already computes one), `filename`,
`description`, `size_bytes`, `sha256`, `created_at`. No `parent_post_id`
(files aren't threaded); no `author` tagged union the way `board_post` has
one — attribution is a local admin-log concern (§18), not something a
catalogue entry needs to assert network-wide, and an uploader's identity
carries no bearing on whether a peer should fetch the bytes. Only an
`area.moderated`-approved, locally-uploaded file is ever queued as a
`file_descriptor` — the identical "never leak a moderation queue onto the
network" rule §9.2 already states for `board_post`. Immutable, single-shot,
like `board_post`/`channel_message` — no edit chain; a changed file is a new
upload with its own new `file_id`, not a revision of an old one.

A receiving node's catalogue materialization is genuinely different from a
carried board's: the *area* becomes a real local `FileArea` row (browsable
via the ordinary `list_file_areas`), but an individual `file_descriptor`
does **not** become a `files` row — `netbbs.files.entries`' own invariant
("a file row is only ever created after its bytes are already safely
written to storage") stays true unconditionally, so a catalogued-but-not-
yet-fetched file cannot live there. It lives instead in a new `remote_files`
table (`file_id`, `area_id`, `origin_fingerprint`, `filename`, `description`,
`size_bytes`, `sha256`, `created_at`, `link_event_json`, `fetched_file_id`)
— catalogue metadata only, browsable and listable (the acceptance
criterion's own "discover and list... without fetching any file content"),
with `fetched_file_id` set only once §11.3's transfer completes and
verifies, at which point the content is promoted into a genuine `files` row
indistinguishable from a local upload for browsing/download purposes.

### 11.3 On-demand chunk transfer

Unlike every other Link mechanism so far, chunk transfer is a direct,
point-to-point pull against one specific peer — the file's own
`origin_fingerprint` (§11.2), never relayed, never gossiped, and not part of
`handle_events`'s dispatch at all: nothing here is a candidate extension of
a shared chain the way board/channel events are, so there is nothing to
verify against a "current origin" the way `board_post_edit` does.

**Wire shape.** A new pair of HTTP routes (`netbbs.link.transport`),
alongside `/events`/`/inventory`: `POST /link/v1/file-chunk/{fingerprint}`.
The **request** is a small, unsigned JSON bundle (mirroring
`InventoryRequest`'s own "not a candidate chain extension, nothing to sign"
reasoning) — `transfer_id`, `file_id`, `chunk_index`, `max_chunk_size`. The
**response** carries the chunk's raw bytes as the literal HTTP body — never
base64-embedded in JSON, per the issue's own explicit requirement — plus a
*signed* `file_chunk_descriptor` (`file_id`, `chunk_index`, `chunk_sha256`,
`chunk_size`, `total_size`, `is_last`, `created_at`), delivered in a response
header (`X-NetBBS-Chunk-Envelope`, base64 JSON) rather than the body, so the
body stays purely raw bytes. Signed by the origin's current signing key —
the same `_resolve_sender_signing_key` resolution every other Link
verification already uses — so a requester can verify the chunk's
authenticity and integrity (`chunk_sha256` against the actual bytes
received) independent of transport-level trust, the same "objectively
verifiable" standard §12.3 holds every piece of Link content to.

**Requester side is required to already be a completed peer** of the
origin (unlike `/inventory`, which serves already-gossiped small metadata to
anyone) — serving arbitrary bytes to an unauthenticated caller is a new
resource-exposure the metadata-only routes don't have, so this route
requires the same "no relay from a stranger" hello precondition
`/events` already enforces.

**Deduplication and resume.** `transfer_id` is deterministic — a content
hash of `(file_id, requester_fingerprint)` — so a retried or resumed fetch
naturally reuses the same id rather than minting a new one every attempt;
the origin uses it to bound concurrent transfers per requester (§13.5's
bounded-remote-influence principle: an explicit `max_concurrent_file_
transfers`-per-peer cap, visible rejection once exceeded, never silent
unbounded growth). `chunk_id` is the chunk's own `sha256` — an exact-content
dedup key, not a sequence number alone: a resent identical chunk (the same
request repeated, or a resumed transfer re-requesting a chunk it turns out
it already has) is recognized as already-applied and skipped rather than
re-written, the same idempotent-resend discipline every gossiped chain
already applies, just against a `link_file_transfer_chunks` row instead of
`known_event_ids`. `link_file_transfers` tracks one row per transfer
(`transfer_id`, `remote_file_id`, `total_size`, `chunk_size`,
`bytes_received`, `status`, staging path) — resuming an interrupted transfer
means asking for the next chunk index past what's already recorded, nothing
more elaborate. Once every chunk is received, the reassembled content's
sha256 is verified against the file's own catalogued claim (§11.2) before
promotion into real storage — a peer claiming a `file_descriptor` with a
hash that doesn't match what it actually serves is refused at that point,
never silently accepted.

**Completed content is stored once by hash.** The verified reassembly is
handed to `netbbs.files.storage.move_temp_file_into_storage` — the exact
same content-addressed layout local uploads already use, so a file fetched
from two different catalogues (or already locally uploaded, coincidentally
identical bytes) shares one stored blob automatically, no special-casing
needed; `remote_files.fetched_file_id` then references the resulting real
`files` row.

**Not a generic work-item/DLQ instance.** Chunk transfer is deliberately
*not* folded into issue #60's outbound-retry abstraction (§13.7) — like
board/channel gossip (§13.7's own "not every retry-shaped mechanism fits"
lesson), it already has a natural resumable-by-construction terminal state
(`link_file_transfers.status`) and no correct "give up" state distinct from
"the requester stopped asking"; a second, differently-shaped retry
abstraction bolted on top would only compete with that.

**Explicitly out of scope for this issue** (mirroring §9.6's own precedent
for channels): file-area origin succession (reused by reference, §9.4's
model, if ever needed); write-back/uploading to a remote area (§11 itself:
"remains owned and stored by its source node"); public/untrusted file
discovery (Phase 4). Inventory/pull catch-up (§8.8) extended to file areas
was left out of this issue too, but is no longer an open gap — see §11.4
(issue #93).

### 11.4 Inventory/pull-based catch-up for file-area catalogues (issue #93)

§8.8's `InventoryRequest`/diff mechanism extends to file-area catalogues
the identical way §9.6 already extended it to channels: a third key,
`file_areas`, alongside `boards`/`channels`, keyed by every `area_id` this
node currently carries (bounded by its own `max_carried_file_areas` quota,
same "request size already bounded by an existing cap" reasoning §8.8
states for boards) mapped to the full set of content IDs already known for
it. The responder's diff (`netbbs.link.store.file_area_event_diff`) unions
the same three sources §8.8/§9.6 already established for boards/channels:
this node's own self-originated genesis (`file_areas.link_genesis_json`,
never routed through `handle_events`), any `file_descriptor` a *local* user
queued regardless of whether this node originated or merely carries the
area (`files.link_event_json`, populated only by self-authorship, per
`netbbs.link.files.queue_file_descriptor_if_linked`'s own scope), and every
peer-received event this node has accepted (`link_events`, filtered by a
new `file_area_id` column, the file-area-scoped counterpart to `board_id`/
`channel_id`). `_handle_inventory` shares one overall `_MAX_EVENTS_PER_
REQUEST` budget across all three diffs now — board, then channel, then
file area, each with whatever remains — not three independent caps.

Only catalogue metadata (`file_area_genesis`/`file_descriptor`) is ever
recoverable this way — this section changes nothing about §11.3's chunk
transfer, which stays a direct point-to-point pull the requester still
must initiate explicitly against the file's own origin once it learns of
a descriptor. A node that recovers a missed `file_descriptor` through an
intermediary carrier therefore ends up with a real, browsable catalogue
entry (`remote_files`, `fetched_file_id` still `NULL`), not fetched bytes
— turning inventory into automatic content replication was explicitly out
of scope for this issue, matching the acceptance criteria's own "recover
metadata/catalogue divergence only" framing.

`file_area_genesis` already went through `netbbs.link.store.save_event`'s
generic dispatch (the same path `board_genesis`/`channel_genesis` use);
`file_descriptor` does not (`materialize_carried_file_descriptor` inserts
its own `link_events` row directly, same shape `materialize_carried_post`/
`materialize_carried_channel_message` already established) — so populating
the new `file_area_id` column needed two call sites, not one, unlike
`channel_id` (only `channel_genesis` needed it, since `channel_message`
does its own insert too, but happened to need no scoped column of its own
until this issue). Restart reconstruction needed no new code: `node.
file_areas` (via `FileAreaEventState`) was already rebuilt from both
`file_areas.link_genesis_json` and `link_events` by issue #89's own
`load_link_node` changes, and `file_descriptor` has no chain state beyond
`known_event_ids`/`events` to rebuild in the first place — the same "no
branch needed" reasoning issue #89 already documented for descriptors.

---

## 12. Trust, reputation, probation, and quarantine

Phase 4 defines the public-network security model. This section is the
normative threat model and policy contract. It does not make a Phase-3 build
safe for public federation by documentation alone; the persistence,
enforcement, operator UI, and tests described here must exist first.

### 12.1 Security goals and attackers

The model protects a node's availability, storage, users, and local policy
without creating a network-wide authority. It must remain safe when facing:

- a malicious user acting through an honest home node;
- a malicious node which signs abuse, lies, selectively relays, or vouches for
  abusive users;
- Sybil identities and colluding nodes;
- a compromised established node or configured trust reporter;
- replaying, withholding, reordering, or selectively forwarding intermediaries;
- partitions, clock skew, stale observations, and compromise recovery.

A signature proves key control, not honesty or independence. A successful dial
proves reachability, not trust. Seeds, discovery, peer introduction, relay
consent, and carrying the same resource confer no reputation.

Every enforcement decision remains local. Nodes may disagree and may override
automatic policy. No signal can force another operator to hide, delete, relay,
or accept anything.

### 12.2 Separate trust dimensions

NetBBS does not compute one scalar reputation score. A local trust view keeps
these dimensions separate:

- **identity and protocol integrity:** signatures, key lifecycle,
  canonicalization, equivocation, and authorization;
- **resource behavior:** flooding, quota evasion, retries, availability claims,
  and relay/storage use;
- **content conduct:** spam, harassment, illegality, off-topic behavior, and
  other moderation judgments;
- **operational reachability:** this node's own dial outcomes.

Operational reachability is routing data only. `netbbs.link.reliability` may
rank dial or relay candidates but must never affect security or content trust.

Node and user trust are separate. Establishing a home node does not establish
every user on it; one abusive user does not quarantine an otherwise honest home
node. A password-only `node_vouched_user` is evaluated as the stable pair
`(home_node_fingerprint, opaque_local_user_id)`, never by display label.

Remote identity attestations have a separate local trust list. Trust-report
authority does not grant age/name-attestation authority, or vice versa.

### 12.3 Local roles, states, and precedence

Establishment and authority to influence policy are different roles:

- a **trust anchor** is explicitly configured by the SysOp;
- an **established identity** has graduated or was established manually;
- a **trusted reporter** is explicitly configured for named dimensions and
  categories;
- a **trust domain** locally groups reporters which may share control or
  incentives.

No role is inferred transitively. A vouch may help probation graduation but
cannot create a trust anchor, reporter, attestation authority, or trust domain.

Per identity and trust dimension, local state is `probationary`, `established`,
`quarantined`, or `blocked`. Manual block has highest precedence, followed by
explicit SysOp overrides, automatic quarantine, then ordinary probation or
establishment. Overrides are scoped, reasoned, timestamped, and audited. Node
sovereignty permits overriding even self-verifying evidence, but the UI must
keep the evidence and risk visible.

### 12.4 Probation and vouching

A remote node starts probationary. Default automatic graduation requires:

- 30 elapsed days since the first verified hello;
- verified direct interaction on three distinct UTC dates;
- no active local integrity or resource trigger;
- active vouches from two trusted reporters in two trust domains.

A remote user starts probationary independently. Default graduation requires:

- 14 elapsed days since first accepting that stable user identity;
- accepted activity on three distinct UTC dates;
- no active trigger in any applicable dimension;
- one authorized user vouch, or explicit SysOp establishment.

A home node's identity vouch binds an opaque user ID to that node; it is not a
behavioral vouch. Probation does not follow a changed home node or signing
identity without a future signed identity-transition protocol.

By default, probation is read-only from the subject's perspective. A
probationary node may complete hello/key-lifecycle exchanges and make bounded
inventory pulls at one quarter of the established-peer request budget. This
node may accept through it content independently signed by an established
author, but refuses or holds for explicit local approval new content authored
or node-vouched by the probationary identity. A probationary node contributes
no trust-signal weight and is not selected to serve as a relay. A probationary
user's posts/uploads enter applicable local approval flow and Link messages are
refused or bounced rather than silently delivered. Private operators may
establish a known node manually instead of waiting for automatic graduation.

Configuration may make these defaults stricter. Relaxing them is an explicit,
audited SysOp safety deviation. Vouches are signed, scoped, expire after at
most 180 days, and may be renewed or revoked. Revocation removes current
support but neither erases history nor accuses the subject of abuse.

### 12.5 Evidence classes and attribution

“Objective” has two classes because not every receiver measurement is
independently provable:

1. **Self-verifying protocol evidence** includes the signed objects needed to
   reproduce the violation, such as conflicting valid extensions of one head.
2. **Observer-attested protocol/resource evidence** records measurements a
   third party cannot reconstruct, such as flooding, malformed requests,
   timeouts, non-delivery, or receipt of an invalid signature.
3. **Subjective content reports** express moderation judgments. Signed content
   may be referenced, but the judgment is still opinion.

Protocol-integrity categories are `signed_equivocation`, `revoked_key_use`,
`invalid_authority`, and `invalid_signature_delivery`. Resource categories are
`malformed_traffic`, `request_flood`, `quota_evasion`, `inventory_nondelivery`,
and `relay_abuse`. Content categories are `spam`, `harassment`,
`illegal_content`, `off_topic`, and `other`. The object version may add
categories later; an unknown category may be retained for diagnostics but has
no automatic policy effect until local software/configuration understands it.

An invalid signature does not prove that its claimed signer created or sent
it. Locally it may justify action against the direct delivery peer; remotely it
remains that observer's claim about the delivery peer. Unsigned malformed
traffic is attributable only to the connection identity available at receipt.

Subjective reports affect only content conduct and can never automatically
trigger transport quarantine. Resource reports affect resource policy;
self-verifying integrity evidence affects integrity policy. UI code must not
collapse these dimensions.

### 12.6 Signed trust signals

A trust signal is an immutable signed object containing protocol/object
version, issuer, stable signal ID, canonical node/user subject, dimension,
category, evidence class, embedded evidence or digest plus locator, observation
and issuance times, expiry, optional explanation, and—when revoking—the exact
signal content ID.

On Link v1 the durable object types are `trust_signal`, `trust_revocation`,
`trust_vouch`, and `trust_vouch_revocation`. They use the ordinary canonical
Link envelope (`netbbs_protocol`, `object_type`, `payload`) plus a detached
base64 signature, but they are stored separately from `link_events` and never
enter content-event flood gossip. `issuer_fingerprint` is the stable node
fingerprint; the detached signature is made by that node's currently
authorized operational signing key. Node subjects contain `kind` and
`node_fingerprint`; user subjects additionally contain `opaque_user_id`.

Evidence has one of two exact forms. Embedded evidence is `{mode: embedded,
data: ...}`. Referenced evidence is `{mode: digest, sha256, size, locator}`.
The signed size may not exceed the evidence limit. A revocation names one exact
`revoked_content_id`, must have the same issuer as its target, and uses the
signal- or vouch-specific revocation type so an ambiguous target lookup cannot
change object-family semantics.

Revocation is a new signed object and never deletes the original. Receivers
reject invalid category/evidence combinations, expiry before issuance, and
issue times over five minutes in the future. Receipt time is retained
independently. Content IDs provide replay deduplication.

Receivers clamp active lifetimes:

- self-verifying protocol evidence: 90 days;
- observer-attested protocol/resource evidence: 7 days;
- subjective content reports: 30 days;
- vouches: 180 days.

Renewal requires a fresh signal. Expired/revoked signals leave automatic policy
but remain under bounded audit retention. Digest-only evidence is not
self-verifying until fetched, size-checked, hashed, parsed, and reproduced;
failure to fetch is not evidence against the subject.

Successfully reproduced self-verifying evidence becomes this receiver's own
local observation. The remote signal's later expiry or revocation removes that
issuer's support but does not un-verify the local observation; its recovery
rule applies independently. Inactive signals and evidence are retained for 365
days by default, unless an active decision or explicit legal/diagnostic hold
still references them. Later pruning preserves the content digest and decision
audit so historical enforcement remains explainable without unbounded blobs.

### 12.7 Propagation, independence, and Sybil resistance

Trust signals are not flood-gossiped with content events. A node explicitly
subscribes to selected reporters and pulls their issuer-signed signals. A
carrier may serve an unchanged signal, but the receiver verifies the issuer
and ignores unconfigured issuers.

The pull is an authenticated Link request rather than durable content. It
binds requester, responder, requested issuer, optional last-content-ID cursor,
page limit, creation time, a 128-bit nonce, and a `revocations_only` containment
flag under the requester's current
operational signing key. The responder requires a completed hello, matching
requester/responder identities, a valid signature, a five-minute freshness
window, and a bounded nonce replay cache. Responses contain only unchanged
stored objects for the requested issuer, ordered by receipt time and content
ID, plus `more_available`. A carrier therefore gains no authority: the request
is addressed to the carrier, while every returned object's independent issuer
signature and local reporter configuration still control admission.
When a configured reporter is quarantined, ordinary subscription sync stops;
the receiver may use only `revocations_only=true`, which serves unchanged
signal/vouch revocation objects and never advances the ordinary subscription
cursor. A manually blocked reporter receives no containment exchange.

Distinct fingerprints do not prove independence. Automatic policy counts
locally assigned trust domains:

- reporters in one domain contribute at most that domain's weight;
- default and normal maximum domain weight is `1.0`;
- remote-signal quarantine requires two domains and total weight `>= 2.0`;
- vouch/reputation paths never multiply weight;
- reporter, domain, weight, and category changes are audited SysOp actions.

An operator may configure a jurisdictional/emergency key as sole authority for
named categories. This explicit local exception is displayed as such; weight
alone never bypasses the two-domain rule.

Default trust-ingress bounds are 100 signals or 1 MiB per response, 1,000
active signals per issuer, 10 active signals per issuer/subject/category, 256
KiB embedded evidence per signal, and a separate ingestion budget in addition
to the ordinary request throttle. Over-limit input is rejected or deferred
visibly, never converted into evidence.

### 12.8 Quarantine effects

Direct local self-verifying integrity evidence may quarantine immediately.
Remote signals must satisfy their configured category rule and the independence
threshold. Observer reports normally tighten limits or extend probation before
quarantine. Subjective reports may hide or moderate named user/content
projections but never quarantine transport automatically.

Node quarantine stops ordinary outbound sync; rejects ordinary events,
inventory, Link messages, files, relays, and peer introductions before new
remote state is persisted; removes the node from relay/candidate selection;
and suppresses applicable locally displayed content. Previously accepted
events, content, identity history, and audits are preserved, not deleted.

A narrow, separately rate-limited containment path may accept verified hello,
key transitions, signal revocations, and recovery metadata. It cannot carry
ordinary content or services. User quarantine affects that user, not unrelated
users, the whole home node, or content merely relayed by the node.

Manual block is harder: it denies even containment until removed. Existing
bytes still are not deleted. Rejections and suppression use stable reason codes
for protocol, diagnostics, and SysOp UI while keeping private reporters, notes,
and policy configuration undisclosed.

The Link HTTP enforcement point runs only after enough cryptographic parsing to
attribute a request, but before remotely influenced persistence or service work.
The normal runtime enables this gate for hello, events, inventory, trust pulls,
file chunks, relay consent/mailboxes, and peer introduction. Peer-list exchange
and file-chunk pulls use authenticated POSTs from completed peers; Link v1
reuses an empty, signed inventory-request authorization envelope so requester,
responder, freshness, nonce replay protection, and current operational-key
verification have one existing definition rather than a second near-identical
request type. The URL fingerprint is routing information, never attribution.
Probationary inventory responses use one quarter of the established event
budget. Valid board posts from probationary users enter the local pending
approval queue; services without an approval projection, including Link mail,
refuse them with a stable reason code.

Enforcement attributes independently signed content to its author/home node,
not to a carrier recorded in `link_events.sender_fingerprint`. Current display
suppression is evaluated from retained signed authorship at read time; changing
or clearing local policy therefore hides or restores projections without
rewriting or deleting the accepted event bytes.

### 12.9 Recovery, partitions, and explainability

Absence, failed dials, and partitions are never evidence. Partitions create no
reports and do not multiply old signals. Signal expiry continues on local time
so an accusation cannot become permanent through disconnection.

Automatic quarantine ends only after every trigger is cleared, expired, or
revoked and a 24-hour recovery hold passes without a fresh trigger. Recovery
returns to probationary, not established. Self-verifying equivocation or
confirmed key compromise also requires SysOp review or verified root-key
recovery; scoped resource/content restrictions may recover automatically.

Effective state is a persisted projection recomputed transactionally on input
changes and startup. For every restriction the SysOp can inspect the subject,
dimension, effects, rule/threshold, evidence, counted domains/weights, times,
overrides, audit history, and requirements for release. Caller-facing behavior
states that local policy restricted content/delivery without claiming a
network-wide verdict or leaking private evidence.

### 12.10 Required validation

Phase-4 implementation must test Sybils in one domain, colluding domains below
and above threshold, compromised reporters, expiry/revocation, replay/stale/
future/oversized signals, reproducible and false evidence, invalid-signature
attribution, subjective-report isolation, partitions and recovery, restart
reconstruction, overrides, preservation without deletion, user/node scoping,
containment recovery, and real SQLite/transport resource bounds.

Public readiness additionally requires a SysOp trust/explanation surface,
manual block/quarantine/recovery workflows, and dogfood with independently
administered nodes. Unit tests alone are not a public-network claim.

### 12.11 Public-readiness checklist (issue #131)

This section is the honest statement §12.10 itself requires: what is actually
validated as of this writing, and what still is not. Update it in place as
coverage changes rather than appending a superseded status below it.

**§12.10's scenario list — validated:**

Sybils in one domain; colluding domains below and above threshold;
compromised-reporter removal (with preservation, not deletion, of the
underlying signal); expiry/revocation; replay (both at the signal level and
the request/nonce level); future-dated signals; oversized signals;
reproducible and false evidence; invalid-signature attribution; subjective
content reports never escalating to transport quarantine; restart
reconstruction; overrides (both application and clearing) as an audited,
reversible transition; preservation without deletion; user/node subject
scoping; containment recovery; and real SQLite/transport resource bounds
(per-subject/category quotas, bounded evidence fetch). All of the above are
covered by tests exercising the real policy/enforcement code paths, most of
them (Sybil weighting, replay/staleness, resource bounds) over a real
loopback transport, not only in-process function calls — see
`tests/test_link_trust.py`, `tests/test_link_trust_wire.py`, and
`tests/test_link_transport.py`.

**Known, accepted gaps in that list** (small, not believed to hide a real
policy hole, but not independently proven either):

- A trust signal that is already past its own declared expiry strictly *at
  the moment it arrives* has no dedicated test distinguishing it from
  ordinary expiry-driven exclusion — it is caught by the same general
  expiry filter every other expired signal is, but that filter's coverage
  of this exact timing has never been asserted directly.
- Trust state under a genuine network partition is validated only as two
  separate proofs, not one combined scenario: `tests/test_link_convergence.py`
  proves generic Link partition/restart convergence with no trust content
  involved, and `tests/test_link_trust.py`'s recovery-hold tests prove
  trust-state recovery timing on a single node with no partition involved.
  Nothing currently drives a real multi-node partition *of trust-affecting
  traffic specifically* through to convergence.

**Public-readiness items:**

- **SysOp trust/explanation surface** — built and reachable through the real
  Telnet menu: subjects, per-dimension state and explanation, override
  application, override clearing, and decision history are all exercised
  end to end through `admin_menu` in `tests/test_admin_flow.py`, not only
  at the `netbbs.link.trust` function level.
- **Manual block/quarantine/recovery workflows** — same real-menu coverage
  for both directions: applying a block through the UI and clearing one
  back to a recomputed state are each tested.
- **One real multi-node exercise covering configuration, quarantine,
  explanation, and recovery together** —
  `tests/test_link_transport.py::test_real_transport_enforces_probation_
  quarantine_block_explains_and_recovers` drives two real nodes over a real
  loopback HTTP connection through the full sequence: probation, an
  established override, escalation to quarantine, escalation to a manual
  block (with the previously-accepted bytes still present and the
  quarantined event still absent), reading the exact SysOp explanation
  surface for that blocked state, clearing both overrides, an explicit
  re-vouch, and a previously-refused push and hello both succeeding again
  over that same connection afterward.
- **Dogfood with independently administered nodes** — **not yet done.**
  Issue #83 tracks this and is deliberately independent in duration from
  this gate (its calendar length does not block the rest of #131), but it
  has not itself happened. This is the one item on this whole checklist
  that automated tests cannot substitute for.

**What this checklist does and does not claim:** every scenario above having
a real, passing test means the *implemented* Phase 4 model behaves
correctly against every adversarial case design doc §12 currently specifies,
including through real transport, storage, and restart. It does **not**
mean NetBBS Link is ready for public, stranger-to-stranger federation —
that additionally requires the independently-administered dogfood above,
and remains explicitly out of scope until it happens. Treat any future
claim of public readiness that does not point back to a completed dogfood
exercise as premature.

---

## 13. Runtime, persistence, and operations

### 13.1 Database execution model

Interactive and background Link work use separate single-worker database lanes,
each with its own SQLite connection and bounded submission depth.

This isolates human-paced foreground work from sustained background federation
traffic while preserving simple synchronous domain functions.

SQLite retains its normal single-writer behavior. `busy_timeout` handles short
cross-lane contention; no application-wide write mutex is introduced.

A cancelled awaiting coroutine does not abort an already-running worker-thread
operation. The database operation completes or rolls back even when the caller
no longer receives the result. Callers must account for that semantic when
performing follow-up state changes.

Shared live `LinkNode` projections are event-loop-owned. A lane-dispatched
function may build and persist events but must not mutate live Link state from a
worker thread.

### 13.2 Atomic invariants

A read-check-write invariant across connections requires one explicit write
transaction:

- begin the write transaction before reading;
- re-fetch current state;
- evaluate safety and no-op conditions from fresh rows;
- write the mutation and audit record atomically;
- roll back on every failure.

The last-usable-SysOp guard is the reference pattern.

### 13.3 Migrations

Migrations are append-only. Never edit a migration which may already have
shipped.

SQLite table rebuilds are dangerous when the rebuilt table is a foreign-key
parent: dropping it can trigger cascade or `SET NULL` actions before the
replacement exists. Prefer `ALTER TABLE ADD COLUMN`, indexes, and explicit
cleanup over rebuilds. When a rebuild is unavoidable, test it against realistic
related rows and the actual dependency graph.

A database from a newer build must fail startup clearly. A matching
`user_version` cannot prove an operator has not manually changed old schema;
manual schema mutation is unsupported unless a future schema fingerprint is
introduced.

### 13.4 Backup and restore (issue #60's first operational slice)

A node's recoverable state is not only its database — it is fourteen
artifacts, today scattered across derived, `db_path`-relative filenames
with no single existing tool that treats them as one recoverable set:

| Artifact | Location | Written by |
|---|---|---|
| Database | `db_path` | every domain write |
| Content blobs | `db_path.parent / f"{db_path.stem}_files"` (git-style `xx/xxxx...` sharding; excludes its own `.incoming/` staging subdirectory, which is always crash-orphan garbage — see `purge_incoming_staging`) | `netbbs.files.storage` |
| Node identity | `identity_dir` (`root.identity`, `signing.identity`, `transport.identity`, `transitions.json`) | `netbbs.link.node_identity` |
| SSH host key | `db_path.parent / f"{db_path.stem}_ssh_host_key"` | `netbbs.net.ssh.ensure_host_key`, once, at first startup |
| Managed-DNS credential | `db_path.parent / f"{db_path.stem}_managed_dns_credential"` | `netbbs.managed_dns.credential`, once, at registration (§16 Decision 7, issue #201) |
| Managed-DNS rename credentials | Previous credential plus the temporary credential-transition journal beside `db_path`; restore preserves both the journal's presence and absence | `netbbs.managed_dns.credential`, during a managed-name transition |
| Welcome banner | `db_path.parent / f"{db_path.stem}_welcome_banner.ans"` | SysOp, via the welcome-banner menu screen |
| Main-menu masthead | `db_path.parent / f"{db_path.stem}_main_menu_banner.ans"` | SysOp, via the masthead menu screen (issue #161) |
| Logoff banner | `db_path.parent / f"{db_path.stem}_logoff_banner.ans"` | SysOp, via the logoff-banner menu screen (issue #177) |
| New-account banner (before signup) | `db_path.parent / f"{db_path.stem}_new_account_banner_before.ans"` | SysOp, via its own menu screen (issue #177) |
| New-account banner (after signup) | `db_path.parent / f"{db_path.stem}_new_account_banner_after.ans"` | SysOp, via its own menu screen (issue #177) |
| Board list masthead | `db_path.parent / f"{db_path.stem}_board_list_banner.ans"` | SysOp, via its own menu screen (issue #176) |
| File area masthead | `db_path.parent / f"{db_path.stem}_file_area_banner.ans"` | SysOp, via its own menu screen (issue #176) |
| Chat channel picker masthead | `db_path.parent / f"{db_path.stem}_chat_channel_picker_banner.ans"` | SysOp, via its own menu screen (issue #176) |

A backup covering only the database silently loses the SSH host key (every
client gets a MITM warning on next connect after restore) and, far more
seriously, the Link node identity (root-key custody is explicitly "part of
ordinary node backup and restore" per §4.5's node identity model, not a
separate ceremony) — so this design treats all fourteen as one atomic backup
operation, never a DB-only one.

**Mechanism**: a new `netbbs.backup` module (synchronous, path-based — no
`Database` wrapper needed, since a backup must be safely takeable against a
*live, running* node, not only an offline one) with two entry points, plus a
`python -m netbbs.backup {create,restore}` CLI in the same spirit as
`python -m netbbs.admin` — deliberately a standalone process rather than an
interactive SysOp-menu action, since backups need to be cron-schedulable
(this project has no background scheduler anywhere, and won't grow one just
for this — matching `_sweep_expired_posts`'s and `files.gc`'s own precedent
of "the operator/an external trigger drives it, not a built-in timer").

`create_backup(*, db_path, identity_dir, destination)`:

1. **Database**: reuses `netbbs.selfupdate.snapshot_database` verbatim
   (`sqlite3.Connection.backup()`, already proven safe against a live WAL
   database in `test_snapshot_and_restore_database_round_trip`) — written to
   `destination/netbbs.db`. Never a raw file copy.
2. **Content blobs**: `shutil.copytree` of the blob root into
   `destination/files/`, `.incoming/` excluded. Must run strictly *after*
   step 1, not before or concurrently — this is what makes the DB-then-
   blobs ordering below actually safe, not just a stated convention:
   `netbbs.files.entries`'s own invariant is that a `files` row is only ever
   created after its bytes are already durably written to storage, never
   the other way around. So every blob a given DB snapshot's rows could
   possibly reference was already on disk before that snapshot was even
   taken — copying blobs afterward is guaranteed to include all of them,
   plus possibly a few newer, still-unreferenced ones from uploads that
   landed in between (harmless — an orphaned blob a future GC pass could
   still reclaim, never a dangling reference). Reversing the order would
   risk the opposite, genuinely broken case: a DB snapshot referencing a
   blob the copy hadn't reached yet.
3. **Node identity, SSH host key, and every banner/masthead singleton**
   (welcome banner, main-menu masthead, logoff banner, both new-account
   banners): plain file copies (each is either static after creation or
   already rewritten via its own atomic-replace pattern — `node_identity.
   py`'s `transitions.json`, notably — so no read-tearing hazard). Every
   banner/masthead singleton is the accepted exception, with no atomicity
   guarantee on its own writes; a backup landing mid-edit could capture a
   half-written file. Accepted as-is: purely cosmetic, no correctness
   consequence, not worth an atomic-write retrofit just for backup's sake.

Writes `destination/manifest.json` last (timestamp, `netbbs.__version__`,
the database's own `PRAGMA user_version`, and a checksum per captured
artifact outside the content-addressed blob tree) — lets an operator (or a
future restore-time check) confirm what a
given backup directory actually is before trusting it. Also records
`last_backup_at`/`last_backup_path` into the live node's own `node_config`
table (same key-value store `netbbs.selfupdate`'s update-check state already
uses) — purely for a future read-only SysOp status line (`_system_menu`,
alongside `[W]elcome`/`[U]pdate`/`[T]imestamp`/`[L]ink status`; letter `K`
for "bacKup", since `B` is already every submenu's universal `[B]ack`), not
required for restore itself.

`restore_backup(*, source, db_path, identity_dir)` reverses each of the
copies above -- **superseded by §13.10's staged/validated workflow (issue
#75)**: the
original mechanism restored each artifact in place, sequentially, with no
validation before the first live path was overwritten and no recoverable
state if interrupted partway. §13.10 replaces the restore side of this
mechanism; `create_backup` and the artifact table above are unchanged.

Restoration always resumes the same node identity; there is still no
supported way to run an old and a restored instance simultaneously -- a
second instance of the same identity already running on a *different*
machine remains an accepted, documented operator responsibility (§13.10's
own PID-file check only ever covers *this* machine).

**Explicitly deferred, not part of this slice**: encrypting backup
contents at rest (identity material is already unencrypted-by-default on a
live node — see §4.5 — and this tool preserves whatever it finds rather
than changing that policy); off-site/remote transport of a completed backup
directory; retention/rotation of old backups; and any form of automatic
scheduling. All are operator/cron responsibilities this tool deliberately
does not take on, the same boundary `files.gc`'s SysOp-triggered-only
design already draws for blob garbage collection.

### 13.5 Bounded remote influence

Every remotely influenced queue, mailbox, retry set, retained-event collection,
transfer, relay store, and bandwidth consumer needs:

- an explicit limit;
- defined backpressure/rejection behavior;
- retry and terminal-failure policy;
- SysOp-visible state;
- safe defaults.

Security state and unread user data must not be silently discarded.

### 13.6 Operational control surface

Issue #60 remains the authority for the incomplete production operating model,
including:

- generic persistent outbound-work items, retry, backoff, dead-letter,
  replay, and cancellation (§13.7 specifies this — `netbbs.link.work_items`,
  implemented, wired into `netbbs.link.mail`/`netbbs.link.sync`, and
  surfaced as an `[O]utbox` SysOp screen);
- sync-lag and historical/trend peer-health visibility (a read-only current-
  state view — peer count/mode, dial-reliability score, last contact, relay
  activity, board/event counters, and relay-mailbox size — is available in
  the SysOp menu's `[L]ink status` screen; per-seed health has nothing to
  show yet, since no per-seed success/failure tracking exists);
- disk, event, mailbox, relay, and bandwidth quotas (§13.9 — peer-count,
  events-per-request, carried-board-count, received-post-size, request-
  body-size, and request-rate quotas, implemented. Event-retention/purging
  and node-wide disk quota are explicitly deferred out of that slice — see
  §13.9's own reasoning);
- integrity checks and crash recovery (§13.11 — a startup `PRAGMA integrity_
  check`, plus confirming migration/incoming-upload/work-item crash safety
  already held by construction — implemented);
- bounded diagnostic log retention without content logging (§13.11 — a new
  `link_diagnostic_log` table and `[D]iagnostic log` SysOp screen, warning-
  level-and-above only, age/row-bounded — implemented);
- protocol/database upgrade and rollback compatibility (§13.11 — the
  database half already done via `netbbs.selfupdate`; the wire-protocol
  half, `netbbs_protocol` version-checked on receipt for the first time,
  implemented);
- graceful drain of Link work during shutdown (§13.11 — `run_link_sync`
  finishes its current pass before stopping, including waking early from
  its own idle interval sleep rather than waiting it out, bounded by the
  existing `graceful_delay_seconds`, falling back to today's hard cancel
  only past that bound — implemented);
- disaster recovery drills exercising a restore under realistic conditions
  (§13.4 specifies the backup/restore mechanism itself — `netbbs.backup`,
  implemented; §13.10 replaces its original restore mechanism with a
  staged, validated, interruption-recoverable one and proves it against
  corrupt/truncated backups, missing components, and mid-switch
  interruption — issue #75, implemented, including a documented drill at
  `docs/NetBBS-disaster-recovery-drill.md`).

An externally operated persistent Link node should not be considered production
ready before these controls exist and have been exercised.

### 13.7 Outbound work items and retry (issue #60's second operational slice)

**Scope decision, made here rather than assumed**: this does *not* uniformly
cover every retry-shaped mechanism in the Link subsystem — only the two that
actually share the same shape. Auditing what exists today:

| Mechanism | Current behavior | Fits a work-item model? |
|---|---|---|
| Board/identity event gossip (`netbbs.link.sync`) | Every node-owned event is unconditionally re-pushed to every seed, every fixed-interval pass, forever — no attempt counter, no per-peer state at all. Safe and cheap only because the receiving side's own dedup (`link_events`) makes redundant delivery free. | **No.** There is no terminal "gave up" state that makes sense — a node's own content should be gossiped for as long as the node exists. Forcing this into a per-target attempt/backoff/dead-letter model would be inventing a failure mode (and per-peer tracking overhead) this mechanism deliberately has never needed. |
| Relay selection/consent maintenance (`_maintain_relay_selection`) | Continuously re-evaluated every pass against an evolving reliability score (`netbbs.link.reliability`), not a single item that must eventually resolve once. | **No.** This is ongoing re-optimization among many candidates, not "keep trying this one specific thing until it succeeds or we give up." It already has its own retry-like model (score-driven re-ranking); wrapping it in a second, differently-shaped abstraction would just be two competing retry policies for the same decision. |
| Link mail delivery (`mail_messages.link_delivery_status`) | Every `'pending'` row is re-pushed to its recipient every sync pass, forever, with **no cap** — the schema already reserves an unused `'expired'` status value for exactly this gap (round 93), never produced by any code path today. | **Yes.** A specific payload to a specific fingerprint that must eventually be confirmed or abandoned — the canonical case. |
| Link mail acknowledgement delivery (`link_mail_acknowledgements.sent_at IS NULL`) | Identical shape and identical gap: re-pushed every pass forever, no cap, no dead-letter. | **Yes.** Same reasoning as mail delivery. |

So `netbbs.link.work_items` is scoped to Link mail delivery and Link mail
acknowledgement delivery only — the two mechanisms that are both (a) a
specific payload addressed to a specific fingerprint, and (b) currently
missing exactly the retry/backoff/dead-letter/inspection issue #60 asks for.
Gossip and relay maintenance keep their existing, already-fit-for-purpose
models unchanged.

**A second scope narrowing, discovered while designing this**: a *work
item* resolving successfully means "the payload was successfully pushed to
the recipient's transport (or deposited at a relay)" — never "the recipient
confirmed receipt." That confirmation, for mail specifically, is a separate,
higher-level thing: `apply_link_message_accepted`/`apply_link_message_
bounced` already handle it, driven by a genuine signed event coming back,
completely unrelated to whether the push itself succeeded. Conflating the
two was a real risk in an earlier draft of this design — a work item is
**"pushed"** or **"dead_lettered"**/**"cancelled"**, never **"delivered"**;
`mail_messages.link_delivery_status` keeps its own independent
`'pending'`/`'delivered'`/`'bounced'` vocabulary, driven by accepted/bounced
events exactly as today. The one integration point is one-directional: when
a `link_mail_delivery` work item dead-letters or is cancelled (the payload
could never even be successfully pushed, or a SysOp gave up on it
manually), the caller — not `netbbs.link.work_items` itself, which stays
completely kind-agnostic — sets `mail_messages.link_delivery_status =
'expired'`, finally giving that reserved value a real producer. A
successfully **pushed** work item changes nothing on `mail_messages`: it
still waits for accepted/bounced exactly as it does today, except the sync
loop stops wastefully re-pushing bytes that already arrived once — a real
efficiency fix, not just new capability.

**Schema** (`link_work_items`, matching this project's established Link-table
conventions — `TEXT NOT NULL` ISO timestamps, a `status` CHECK-constraint
enum, a partial index on the still-pending predicate):

```sql
CREATE TABLE link_work_items (
    id                  INTEGER PRIMARY KEY,
    kind                TEXT NOT NULL,  -- 'link_mail_delivery' | 'link_mail_ack'
    reference_id        TEXT NOT NULL,  -- mail_messages.link_event_content_id, or the ack row's own id
    target_fingerprint  TEXT NOT NULL,
    status              TEXT NOT NULL
                        CHECK (status IN ('pending', 'retrying', 'pushed', 'dead_lettered', 'cancelled')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    last_attempt_at     TEXT,
    last_error          TEXT,
    resolved_at         TEXT,
    UNIQUE(kind, reference_id, target_fingerprint)
);

CREATE INDEX idx_link_work_items_due
    ON link_work_items(next_attempt_at) WHERE status IN ('pending', 'retrying');
```

`reference_id` is a pointer, not a payload copy — `netbbs.link.work_items`
never stores or looks at the actual signed event bytes; the caller (mail
delivery/ack-push code in `sync.py`) already has those via the referenced
row and is the only thing that knows how to actually attempt the push.

**State machine**: `pending` (never attempted) → `retrying` (attempted at
least once, not yet resolved) → one of `pushed` / `dead_lettered` /
`cancelled` (terminal). `enqueue_work_item` is idempotent on
`(kind, reference_id, target_fingerprint)` — creating a mail message or an
acknowledgement always enqueues its work item at that same moment, so
there's no separate "did we remember to schedule this" step to forget.

**Backoff and dead-letter thresholds** (product judgment, not derived from
anything load-bearing — adjustable later): each failed attempt schedules
the next one at `min(300s * 2^attempts, 6h)` (starting at the sync loop's
own default interval, since backing off faster than the loop even runs is
meaningless, doubling from there, capped at six hours so a long-unreachable
target still gets retried a few times a day rather than trailing off to
nothing). Dead-lettered once `attempts >= 10` **or** `now - created_at >= 5
days`, whichever comes first — the attempts cap is what actually fires in
the common case (already ~29 hours of real spacing by the tenth attempt);
the age cap is a safety net for a node that was itself offline for a
stretch and so accumulated attempts slower than wall-clock time would
suggest.

**Mechanism**: no new background loop. `netbbs.link.sync.run_link_sync`'s
existing fixed-interval loop already iterates several independently-loaded
"pending work" lists every pass (seeds, pending mail, pending
acknowledgements, relay candidates) — `_push_pending_link_mail`'s and the
acknowledgement-pushing code's own `load_pending_link_mail`/
`load_pending_link_mail_acknowledgements` calls are replaced by
`load_due_work_items(kind=...)` (status in `pending`/`retrying` *and*
`next_attempt_at <= now`), and each attempt's outcome is recorded via
`record_success`/`record_failure` instead of silently falling through to
"try again next pass regardless." Everything else about the loop — lane
dispatch, per-item try/except-log-and-continue tolerance, the fixed sleep
— is unchanged.

**SysOp surface**: `list_work_items` (filterable by status/kind) plus
`replay_work_item`/`cancel_work_item` (both audit-logged via
`record_action`, matching every other SysOp-triggered mutation in this
codebase) — a new admin-menu screen, most naturally alongside `[L]ink
status` under `System`, listing dead-lettered/retrying items with a picker
to inspect one and replay or cancel it. `replay_work_item` resets a
`dead_lettered`/`cancelled` item to `pending` with `attempts = 0` and, for
`link_mail_delivery`, is the one place the caller undoes the
`mail_messages.link_delivery_status = 'expired'` side effect back to
`'pending'` — symmetric with how dead-lettering set it.

**Explicitly deferred, not part of this slice**: retention/purge of old
`dead_lettered`/`cancelled`/`pushed` rows (this table will otherwise grow
without bound — a real gap, but a generic "how long do resolved audit-
shaped rows live" question this project doesn't have an established answer
to yet, not specific to work items); applying this abstraction to any
future work kind beyond the two named here without first checking it
actually fits the shape (see the scope-decision table above — the fit
matters more than the count of kinds). The quotas/integrity-check/
log-retention/upgrade-rollback/graceful-shutdown bullets in §13.6, separate
pieces of issue #60, were open when this slice was written and have since
been closed by §13.9/§13.11.

Implemented: `netbbs.link.work_items` (schema, backoff/dead-letter,
replay/cancel, all audit-logged); `netbbs.link.mail.compose_link_message`/
`_queue_acknowledgement` enqueue a work item in the same transaction as
the row it tracks; `netbbs.link.sync._push_pending_link_mail` now attempts
only currently-due work items instead of unconditionally resending every
pending row every pass, and already skips a push entirely once a message
has resolved through some other path (a genuine accepted/bounced event, or
an earlier dead-letter); a new `[O]utbox` SysOp screen (`System` submenu,
gated on Link being configured, same as `[L]ink status`) lists
retrying/dead-lettered items and lets a SysOp replay or cancel one.
Verified end to end via `tests/test_link_sync.py`'s existing real-socket
sync tests (unchanged, still passing against the refactored push loop)
plus new dedicated tests for the state machine, the mail/ack integration,
and the SysOp screen.

### 13.8 Session lockdown, drain, and shutdown

Three related but distinct SysOp `[N]ode`-menu controls over who can
connect and who stays connected, each answering a different question:

| Control | Question it answers | New logins | Already-connected sessions | Reversible | Ends the node process |
|---|---|---|---|---|---|
| `[S]hutdown` | "Take this node down" | Blocked for everyone, no bypass | Warned (staged), then all disconnected (immediately, or after an operator-chosen grace period) | Yes, while still counting down — see below | Yes, once the countdown finishes |
| `[M]aintenance mode` | "Stop admitting ordinary users for now" | Blocked for non-SysOps; a SysOp can still log in | Untouched | Yes — toggle again | No |
| `[D]rain` | "Clear ordinary users off, right now" | Unaffected (not this control's job — see the reminder below) | Non-SysOps warned (staged), then disconnected after an operator-chosen delay; SysOps (including the issuer) untouched | Yes, while still counting down | No |

`[S]hutdown`'s own sequence (round 51) already had the right order in
code — lock out new logins and warn everyone immediately, then disconnect
once the delay elapses — but its confirmation prompt's wording had drifted
out of sync with that, describing disconnect happening *before* the
lockout; fixed to match the actual, unchanged behavior.

**`[M]aintenance mode`** (`netbbs.net.maintenance.MaintenanceMode.
enable_lockdown`/`disable_lockdown`/`is_lockdown_active`) is a second,
independent flag on the same class that already holds shutdown's
`activate`/`is_active` — deliberately not the same flag, and deliberately
checked at a different point in the connection lifecycle: shutdown's gate
fires before login even begins (nothing is known yet about who's
connecting, so no bypass is possible or desired — the whole node is going
away regardless); lockdown is checked *after* credentials verify
(`netbbs.net.login_flow.run_authenticated_session`), specifically so a
SysOp can still reach the menu that turns it back off. A SysOp who logs in
while it's active sees a `(Maintenance mode is ON.)` notice appended to
their welcome line; a non-SysOp sees `LOCKDOWN_MESSAGE` and is disconnected
before ever reaching the main menu. Turning lockdown on does nothing to
sessions already connected — that is `[D]rain`'s job, a deliberately
separate action, not an implied side effect. The `[D]rain` screen's own
intro text now says so explicitly too (issue below): draining alone does
not block new non-SysOp logins, only `[M]aintenance mode` does that — a
real point of confusion in practice (Thiesi's own dogfood-testing report:
a SysOp assumed draining alone would leave the node empty afterward, when
in fact it only guarantees an empty node at the one instant the disconnect
actually fires).

**`[D]rain`** (`netbbs.net.shutdown.run_drain_sequence`) borrows
`run_shutdown_sequence`'s warn-then-disconnect shape but never touches
`maintenance`/`shutdown_event` — the node keeps running throughout.
`ActiveSessionRegistry.broadcast_to_all`/`disconnect_all` both gained an
`exclude_sysops` parameter (backed by a new `is_sysop` flag recorded on
each session's entry at `mark_authenticated` time) so a SysOp, including
whoever issued the drain, is never warned or disconnected by it — the
whole point is staying connected to keep managing the node while ordinary
users clear out for a change that needs a reconnect to take effect.

**The intended workflow** (Thiesi's own framing): turn on `[M]aintenance
mode` first — if nobody else is online, or existing sessions don't need
disturbing, that alone is enough. If someone connected already needs to be
moved along, follow up with `[D]rain`. The two are composable, not
coupled: `[D]rain` never enables lockdown itself, and enabling lockdown
never triggers a drain — each is a deliberate, separate SysOp decision,
matching how follow/membership/node-carry are kept independent elsewhere
in this design (§6.6) rather than one silently implying another.

#### 13.8.1 Scheduling, cancellation, staged reminders, and visibility (round-trip closed after real dogfood use)

A real multi-day dogfood run of the three controls above (§17's own
"run it for real, not just test it" mandate) surfaced five concrete UX
gaps, all closed together since they share one root cause and one fix:

**The bug.** `[D]rain`/`[S]hutdown` each launched their sequence as a bare
`asyncio.create_task(...)` with nothing tracking that one was already in
flight. Running the same command twice launched two independent,
uncoordinated countdowns racing each other — a second, shorter delay could
disconnect everyone while a reconnecting user still had to wait out
whatever remained of the *first*, now-orphaned countdown too, with zero
visibility into any of it.

**The fix: `netbbs.net.shutdown.SequenceScheduler`.** One instance per
node *per sequence kind* (`drain_scheduler`, `shutdown_scheduler` —
constructed in `netbbs.__main__` alongside `session_registry`/
`maintenance`, threaded the same way) tracks at most one currently
in-flight sequence: its deadline, its custom message (if any), and the
`asyncio.Task` actually running it. `schedule()` always cancels-and-
replaces any existing one first — the actual fix for the stacking bug.
Deadlines are `asyncio.get_running_loop().time()`-based (monotonic,
process-local); nothing here needs to survive a restart or be compared
across processes. A signal-triggered shutdown (SIGTERM/SIGINT) registers
with the same `shutdown_scheduler` a live SysOp session's `[S]hutdown`
command would use — one shared source of truth regardless of what
triggered it, so a connected SysOp sees accurate status either way.

**Provenance and cancellability (issue #108, revised after this
subsection originally shipped).** Each tracked sequence also records
`source` (`"sysop"`, `"sigterm"`, `"sigint"`) and `cancellable`, both
defaulting to the SysOp-created shape every pre-existing caller already
had. `netbbs.__main__._install_signal_handlers` is the only caller that
passes `cancellable=False` — a service supervisor's SIGTERM/SIGINT
outranks an in-BBS choice to keep the node running, so a connected SysOp
must never be able to cancel (or, equivalently, silently *replace*) a
shutdown that supervisor triggered; escalating to SIGKILL if NetBBS kept
running anyway would defeat the whole point of a graceful stop.
`SequenceScheduler.cancel()` — the explicit "cancel it?" action — refuses
outright for a non-cancellable sequence; `schedule()` itself stays
unconditional regardless (a second real SIGTERM must still be able to
reset an in-flight SIGTERM countdown), so `netbbs.net.admin_flow.
_shutdown_screen` is the one responsible for never reaching its own
`schedule()` call in that case either — a non-cancellable, already-
scheduled shutdown gets a status-only message and returns immediately,
never the ordinary "schedule a new one" prompts. A SysOp-created shutdown
remains fully cancellable/replaceable exactly as this subsection
originally described.

**`[L]ock & drain` ownership (issue #109).** `[M]aintenance mode` and
`[D]rain` are deliberately separate, composable primitives; `[L]ock &
drain` (added after this subsection originally shipped) just composes
them for the common case a SysOp wants both together. Its own screen
(`netbbs.net.admin_flow._lock_and_drain_screen`) must not infer whether
*it* is active from `maintenance.is_lockdown_active()` alone — that bit
is shared with the plain `[M]` toggle, so a lockdown enabled
independently would otherwise be misreported as "Lock & drain already
active," refusing to start the requested drain at all (the concrete
dogfood-adjacent bug this closes). `MaintenanceMode.enable_lockdown()`
therefore also records `source` (`"maintenance"` for the plain toggle,
`"lock_and_drain"` for the composite command — the same provenance
concept as the scheduler's own `source` above, reused rather than
inventing a parallel mechanism, exactly as the issue asked), and the
composite's own drain is tagged `source="lock_and_drain"` on
`drain_scheduler` too. The screen only ever reports itself "active," or
offers to undo anything, when it actually owns the lock
(`lockdown_source() == "lock_and_drain"`) — and even then only cancels
the drain half if it owns that too, never a drain some other, later,
independent action scheduled while its own lock was still up. Lockdown
already on for an unrelated reason is left completely untouched; the
composite command only ever adds a drain on top of it, never reclaims
or later disables it.

This one piece of state is what makes the remaining four gaps closable as
straightforward reads/writes against it, not four separate mechanisms:

**1. Explicit cancel-or-replace, not silent stacking.** Re-running
`[D]rain`/`[S]hutdown` while one is already scheduled now shows its
remaining time and offers "Cancel it?" before proceeding (unless it's a
non-cancellable signal-triggered shutdown — see the provenance paragraph
above, which gets a status-only message and an immediate return instead)
— answering yes cancels cleanly and stops; answering no continues into
the ordinary
prompts, and the resulting new schedule replaces the old one via
`schedule()`. `[S]hutdown` also gained a per-invocation delay prompt for
the first time (previously a fixed `graceful_delay_seconds` config value
with no override) — it now behaves exactly like `[D]rain`, Thiesi's own
explicit ask to close a "these two feel like different features" mental
disconnect that had never been intentional, just an artifact of the two
having been built in separate rounds. The config value is now only the
*prefill default* for the prompt, and what the SIGTERM/SIGINT signal path
still uses (no one to prompt there).

Cancelling a *scheduled* graceful shutdown needed one real design
decision: `MaintenanceMode.activate()`'s own docstring already stated "no
way back" — true once a shutdown reaches its actual disconnect step, but
no longer true for the countdown window before that, now that the window
is cancellable. `MaintenanceMode.deactivate()` is the one narrow
exception, called only from `run_shutdown_sequence`'s own
`except asyncio.CancelledError: maintenance.deactivate(); raise` handling
around the graceful countdown — reopens new-login admission if (and only
if) the countdown itself is what got cancelled, never after
`disconnect_all()` has actually run.

**2. Staged countdown broadcasts, Unix-`shutdown`-style.** Both sequences
now broadcast on schedule, then again at 5 minutes remaining and 1 minute
remaining (only the ones the total delay actually reaches — a 30-second
drain never fabricates a "5 minutes remaining" reminder it could never
pass through), then once more immediately before disconnecting —
`netbbs.net.shutdown._run_staged_countdown`, shared by both. A custom
message, if given, is broadcast verbatim at every stage rather than
varying with the remaining-time phrase — consistent with the existing
"message replaces the default text entirely" rule (Thiesi's own wording,
unchanged from before this round), just now applied at more than one
point in time.

**3. A freshly-connecting or freshly-logged-in user is told what a
one-off broadcast alone never could.** Before this round, drain state was
purely a one-shot broadcast — a user not connected at the moment it fired
(including one reconnecting *after* an earlier drain pass already
disconnected them) had no way to know a drain was still in progress until
it silently disconnected them again. Now: `netbbs.net.login_flow.
run_authenticated_session` tells a non-SysOp, once, right after login
(the same one-time-notice convention `_announce_pending_invitations`
already established), that a drain is scheduled and roughly how long until
disconnection — SysOp-exempt, since drain never actually affects them.
Separately, `netbbs.net.maintenance.LOCKDOWN_NOTICE` (deliberately
distinct wording from `LOCKDOWN_MESSAGE`, the actual non-SysOp rejection)
is shown to *every* connecting client right after the welcome banner,
before credentials are even checked — SysOp-ness isn't known yet, so this
can't be targeted any more narrowly, and a SysOp who's about to
successfully log in anyway must not be told "please try again later." The
existing hard pre-login `MAINTENANCE_MESSAGE` (shutdown's own unconditional
gate) also gained the scheduled shutdown's own remaining time, read from
`shutdown_scheduler`, when one is available.

**4. A visual, persistent indicator — not just a one-time line easy to
scroll past or never see at all.** The real dogfood incident this closes:
a SysOp turned maintenance mode on, then forgot, and only found out when a
user reported being unable to log in. `netbbs.net.login_flow.
_draw_main_menu`'s own `Choice: ` prompt (given a live `node_controls`) now
carries a compact prefix: the current BBS time (a snapshot at draw time,
deliberately not a ticking live clock — this codebase has no per-session
background refresh mechanism, and building one just for a clock would be
disproportionate to what was actually asked for), plus at most one alert
tag, most urgent first — `[SHUTDOWN M:SS]`, else `[DRAINING M:SS]`, else
(SysOps only, by construction: a non-SysOp who reached the menu at all
already implies lockdown isn't blocking them) `[MAINT MODE]`. The `[N]ode`
admin menu itself also gained an unconditional status line (maintenance
on/off, plus either scheduled control's own remaining time) — the SysOp's
own dashboard for the exact "did I leave this on" question that prompted
the whole fix. `netbbs.rendering.ALERT_COLOR` (a new palette entry) marks
all of this consistently, distinct from `PRIVILEGE_COLOR` (an account's
own permanent access badge) and `MUTED_COLOR` (routine informational
text) — this specifically means "something time-sensitive is happening to
the node itself."

**5. Operator-visible in stdout/the process log, not only the DB-backed
moderation log.** Scheduling or cancelling a drain/shutdown, and toggling
maintenance mode, now each log one `INFO`-level line
(`netbbs.net.admin_flow`'s own `_logger`) alongside the existing
`record_action` audit row — an operator watching a foreground terminal or
`journalctl` sees it happen without a separate DB query. `netbbs.__main__.
run()` also gained one `"NetBBS is ready to accept connections"` line,
logged once every configured listener/background task has actually
started successfully (immediately before the process blocks on
`shutdown_event.wait()`) — previously each transport only logged its own
"listening on..." line individually, with no single line marking that
startup as a whole had actually finished.

### 13.9 Quotas: closing the remaining bounded-remote-influence gaps (issue #60's third operational slice)

**Audit before design, same discipline §13.7 used for work items.** §13.5
already states the general rule (every remotely influenced resource needs an
explicit limit, defined rejection behavior, and SysOp visibility); this
section is the concrete audit of where that rule is and isn't met yet, and
the scope decision for what this slice actually closes.

**Already bounded, no change needed here** — peer-list entries per request
(`_MAX_PEER_LIST_ENTRIES_PER_REQUEST = 100`), unverified candidate
descriptors (`_MAX_CANDIDATE_DESCRIPTORS = 500`, new-fingerprint admission
capped but refreshing an already-tracked candidate is always allowed),
relay-serving slots (`max_relay_clients`, decline-not-error), relay mailbox
envelopes per recipient (`MAX_MAILBOX_ENVELOPES_PER_RECIPIENT = 50`, HTTP 507
on overflow, no eviction), Link mail delivery/acknowledgement retry
(§13.7's backoff-then-dead-letter), local mailbox size (`MAX_MAIL_PER_
RECIPIENT`, evict-oldest-read/refuse-if-all-unread — already applied to
incoming Link mail too, bouncing rather than silently dropping), and Zmodem
upload size (`max_upload_bytes`, checked against both claimed and actual
running size).

**Gaps this slice closes** — every currently-uncapped *admission* point in
the Link protocol, plus the two content-validation gaps that most directly
let one peer impose unbounded cost on another node:

| Gap | Fix | Enforcement idiom |
|---|---|---|
| `LinkNode.peers`/`link_peers` — any node that completes a hello becomes a permanent peer, no cap (mirror-image gap to candidate descriptors, which *are* capped) | New `LinkConfig.max_peers` (default 1000 — generous relative to §14's declared small-network scale, but no longer infinite) | Same shape as candidate descriptors: admitting a genuinely *new* fingerprint past the cap is refused; a hello from an *already-known* peer (key rotation, descriptor refresh) is always accepted regardless of the count. Implemented as `handle_hello`'s own optional `max_peers` keyword (`None` default, unbounded, preserving every prior caller) rather than `handle_relay_consent_request`'s "caller decides" split — that idiom exists specifically for relay-consent's in-band `accepted=False` reply shape, which `handle_hello` has no equivalent of; refusing a hello is a whole-request failure either way, the same shape peer-list's own internal cap already uses. Only threaded into the *inbound* path (`LinkServer._handle_hello`/`_handle_relay_mailbox_pickup`) — `dial_hello` (outbound) is left unbounded by this cap, already indirectly bounded by `_MAX_CANDIDATE_DESCRIPTORS` plus the operator's own small configured seed list. |
| `handle_events` batch size — no per-request cap, unlike peer-list's own `_MAX_PEER_LIST_ENTRIES_PER_REQUEST` from the same design round | New `_MAX_EVENTS_PER_REQUEST = 200` beside the existing constant in `protocol.py` | Reject the whole batch with `LinkProtocolError`, identical to the peer-list precedent — a genuine sync backlog still drains over several passes rather than one unbounded request. |
| `board_post`/`board_post_edit` content size — zero validation on receive, unlike locally created posts (`netbbs.boards.posts.MAX_SUBJECT_BYTES`/`MAX_BODY_BYTES`) | Apply the same two constants inside `handle_events`'s `board_post`/`board_post_edit` branches | `LinkProtocolError`, matching every other malformed-event rejection already in that method. |
| Carried-board count — `materialize_carried_board` turns any verified `board_genesis` into a local `Board` row unconditionally | New `LinkConfig.max_carried_boards` | The `board_genesis` event is still verified, accepted, and gossiped on past this node (dedup and chain integrity for *other* nodes must not depend on this node's own local storage choices) — only *materializing* a local, browsable `Board` row is refused once the cap is hit. This is the exact shape §9.3 already specifies for a local exclusion: represented honestly as "not carried on this node," never a silent, indistinguishable drop. |
| Link HTTP request body size — neither `LinkServer` nor `WebServer` sets `client_max_size`, so both silently inherit aiohttp's implicit 1 MiB default | Set `client_max_size` explicitly on `LinkServer`'s `web.Application()`, sized to comfortably fit `_MAX_EVENTS_PER_REQUEST` worth of events (2 MiB) | Turns an accidental library default into a deliberate, documented value; aiohttp's own 413 response is unchanged (not worth reshaping into a `LinkProtocolError` payload for a request that was rejected before any handler ran). |
| Link HTTP request rate — no throttling on any Link route at all, including the two unauthenticated ones (`/hello`, `/peers`) | New `netbbs.net.throttle.LinkRequestThrottle` (a small public wrapper around the existing `_KeyedTokenBuckets` machinery `LoginThrottle` already uses internally), keyed by source address, applied via an aiohttp middleware on every route -- constructed once in `netbbs.__main__` from three new flat `LinkConfig` fields (`request_rate_capacity`/`request_rate_refill_per_minute`/`request_rate_max_tracked_sources`, not a nested sub-dataclass) and passed into `LinkServer`, the same "build once, node-lifetime, threaded into the one real server" shape `_build_throttle` already uses for `LoginThrottle` | Exceeding it returns a plain HTTP 429, no signed payload needed (an unauthenticated-route response can't be signed meaningfully anyway). `None` throttle (every caller predating this) is a middleware no-op, not a hard requirement. |

**Explicitly deferred, not part of this slice** — following §13.7's own
precedent of naming what's excluded and why, rather than silently narrowing
scope:

- **`link_events` retention/purging.** The two places this gap already exists
  in code (`storage/migrations.py`, `link/store.py`) both name the same
  blocker themselves: purging has to reckon with `handle_events`'s own
  chain-idempotency self-heal logic for `key_transition`/`board_post_edit`/
  lifecycle events first, or a purge could resurrect a fork-detection false
  positive for an event this node legitimately already integrated. That is
  its own design pass, not a quota default.
- **Node-wide disk quota on blob storage** — the literal "disk" word in issue
  #60's own bullet, and still the single largest true gap (no `shutil.disk_
  usage`, no running byte counter, no code anywhere aware of aggregate
  storage consumption). Needs genuinely new disk-usage-tracking machinery
  this codebase doesn't have yet, not a threshold check against an existing
  number — sized as its own future slice rather than folded in here.
- **`link_work_items` terminal-row retention.** Locally driven growth (a
  node's own composed mail history), not remote-influenced admission — pairs
  more naturally with issue #60's separate "log retention" bullet than with
  quotas, and every individual item is already bounded by dead-lettering.
- **Zmodem transfer-rate (time) limiting.** The existing byte-size cap plus
  `ThrottleConfig`'s connection/session limits already bound the worst case
  tolerably; true transfer-rate instrumentation isn't worth building until a
  real problem is observed.

**SysOp visibility.** `[L]ink status` gains a peer-count line showing
`current/max_peers` (matching the existing `relaying_for`-slots-in-use
display precedent) and a carried-boards `current/max_carried_boards` line;
rate-limit rejections are logged the same way `LoginThrottle` rejections
already are, not surfaced as a separate screen in this slice.

Implemented.

### 13.10 Staged, validated restore (issue #75)

**Problem, confirmed by reading the code, not assumed from the issue alone**:
the original `restore_backup` copies each of the five artifacts (§13.4)
straight into its live path, sequentially, in place -- `shutil.copy2`/
`copytree` directly onto `db_path`/`identity_dir`/etc., with the previous
live directory `shutil.rmtree`d immediately before the replacement copy
starts. Nothing about the backup is checked before the first live path is
touched (the manifest's own fields today are metadata only -- no
checksums), and an interruption mid-copy leaves neither the old state (already
half-deleted) nor a complete new one -- exactly the "restore intended to
recover a node can make it less recoverable" failure the issue describes.
The write-lock probe (`_require_not_in_use`) also only ever catches a
transaction genuinely in flight at that instant, not an idle-but-running
node holding no lock between transactions -- its own docstring already
said so.

**Mechanism**: validate everything first, stage a full copy, then switch
staged artifacts into their live paths with atomic renames -- never restore
by copying directly onto a live path again.

**1. Manifest gains per-artifact checksums.** `create_backup` now writes
`manifest["checksums"] = {relative_path: sha256_hex}` for every file it
captures *outside* the content-addressed blob tree: the database snapshot,
each of the four identity files, the SSH host key, and the welcome banner.
The blob tree needs no manifest entry at all -- `netbbs.files.storage`
already lays every blob out at `root/{sha256[:2]}/{sha256}`, so a blob's own
path *is* its claimed hash; restore verifies the tree by recomputing each
blob's hash and checking it against its own filename, catching truncation/
corruption with no extra bookkeeping and no manifest growth as a node's
file area grows.

**2. Full validation before any live path is touched.** A new
`_validate_backup_source(source) -> Manifest`, called first, unconditionally:
manifest exists and parses; every checksummed file listed is present and its
hash matches; the database snapshot passes `PRAGMA integrity_check` *and*
opens cleanly as a real `netbbs.storage.database.Database` (this reuses,
rather than reimplements, that class's own existing "refuse a schema newer
than this build supports" guard from `_apply_migrations` -- restoring a
backup taken by a newer NetBBS version onto an older install was already a
real risk this makes checked, not just checked-if-someone-remembers); the
identity directory, if present, actually loads via `netbbs.link.node_
identity.NodeIdentity.load` (a genuine functional check -- chain-to-key
consistency and all -- not just "the files exist"). Any failure raises
`BackupError` with the specific problem before a single live byte moves.

**3. Node-liveness check gains a PID file, kept as a second layer alongside
the existing lock probe, not a replacement for it.** `netbbs.__main__`
writes its own PID to `db_path.parent / f"{db_path.stem}.pid"` once
started, removed in the same `finally` that already closes the database on
every exit path (SIGTERM, SIGINT, and startup failure alike). Restore reads
this file if present and checks the PID is still alive with a portable,
best-effort liveness check (`os.kill(pid, 0)` on POSIX; a `tasklist` shell-
out on Windows for local dev/test convenience -- the deployment target is
NetBSD, where the POSIX path is what actually matters) -- refuses if alive,
catching the idle-but-running case the lock probe alone could not. A PID
file present but pointing at a dead process is treated as a stale leftover
from an unclean exit (warn, proceed) rather than a hard refusal -- the same
"an operator responsibility, not a load-bearing distributed lock" framing
this section already applies to the cross-machine case.

**4. Stage before touching anything live.** Every artifact is copied (with
its checksum reverified against the fresh copy, catching corruption
introduced by the staging copy itself, not just the original backup) into
`db_path.parent / f".netbbs-restore-staging-{token}"` -- a sibling directory
on the same filesystem as the live targets, which is what makes step 5's
renames atomic rather than a second copy.

**5. Switch via rename, not copy, with a non-silent marker for the gap
between the first and last rename.** Before switching anything, restore
writes a small state file (`db_path.parent / ".netbbs-restore-state.json"`)
naming the staging directory, a per-token rollback directory, and which
artifacts remain to switch. For each of the five targets in turn: rename
the current live artifact (if any) into `db_path.parent /
f".netbbs-restore-rollback-{token}"` under its own name, then rename the
staged artifact into the live path, updating the state file after each
completed step. If any single rename fails, everything already switched is
renamed back from the rollback directory (best-effort, since the renames
already succeeded once and are switching back onto paths that still exist)
before re-raising -- recovering the previous generation automatically in
the common case. The state file is removed only once every artifact has
switched (success) or every switched artifact has been rolled back
(recovered failure); if the process is killed outright mid-switch rather
than raising a catchable exception, the state file survives as the "clearly
identified... not a silent mixture" record the acceptance criteria asks
for, and a subsequent `restore` invocation refuses to start a new one over
an unresolved marker rather than compounding the mess.

**6. The rollback generation is not auto-deleted on success.** A completed
restore leaves `.netbbs-restore-rollback-{token}` on disk holding the
*previous* live state, not silently discarded -- matching this project's
"never silently discard state a human might still need" stance (§13.5) and
`netbbs.selfupdate`'s own "kept on disk, rotated out" precedent for a
superseded release directory. The CLI prints its path; cleanup is an
explicit operator/cron action (retention/rotation stays out of scope here,
same as §13.4's own already-deferred list), not automatic.

**Disaster-recovery drill.** Documented at `docs/NetBBS-disaster-recovery-
drill.md`: stop the node; corrupt or truncate a real backup and confirm
restore refuses before touching anything live; interrupt the restore
process mid-switch and confirm the previous generation is intact or the
state file clearly names what to do; complete a real restore and confirm
identity continuity (same fingerprint), every configured transport still
authenticates, previously created local content is still browsable, and
Link resumes gossiping with its peers on restart. Proven functionally,
live, against a real running/killed/restarted node process during
implementation -- corruption refusal (checksum mismatch caught before any
live byte moved, confirmed via before/after hashing), the PID-file check
against a genuinely running process, a stale PID file from a hard-killed
process correctly tolerated rather than blocking restore, and a full
restore-then-restart cycle with identity/content verified. Running the
drill specifically on NetBSD hardware remains the one piece this design
enables but does not itself execute from a non-NetBSD development
environment.

Implemented.

### 13.11 Closing issue #60: integrity, diagnostics, protocol compatibility, graceful Link drain

Four remaining, previously-open bullets from §13.6 — audited individually
below, same discipline §13.7/§13.9 already used, each narrower in places
than its one-line issue wording once actually read against the code.

**1. Startup integrity check and crash recovery.** Confirmed by grep: `PRAGMA
integrity_check` exists nowhere in this codebase except inside issue #75's
own backup-validation path — an ordinary node startup opens the database
with no corruption check at all, so a corrupted file (disk failure, an
interrupted non-WAL filesystem operation, bit rot) surfaces only later, the
first time some unlucky query happens to touch the damaged page, as a raw,
confusing `sqlite3.DatabaseError` rather than a clear diagnosis at the one
point an operator can still act on it before real damage compounds.
`netbbs.__main__.run()` now runs `PRAGMA integrity_check` immediately after
`Database(config.db_path)` opens, wrapped into the same `StartupError`
message shape that already handles "wrong build/version" — refusing to
serve traffic against known corruption, matching round 56's own "refuses to
start with zero SysOps" precedent for a startup condition worth failing
loudly on rather than limping past. **Deliberately not folded into
`Database.__init__` itself** — a full-database scan on *every* `Database()`
construction would tax every admin script and the entire test suite (2500+
constructions) for a check only the one long-lived node process actually
needs once, at its own startup; `netbbs.__main__` calls it explicitly, once,
itself.

Crash recovery beyond that single check turns out to already exist,
confirmed by reading rather than assumed: `_apply_migrations` commits a
migration's schema change and its `user_version` bump in the *same*
transaction, so a crash mid-migration simply leaves `user_version`
unadvanced — the next startup resumes migrating from the correct point,
never re-applies a partial migration, never needs new code. `purge_incoming_
staging` already treats every leftover `.incoming` file as crash debris from
a previous run and removes it before any listener starts. `netbbs.link.
work_items` are DB-row-backed with their own retry/backoff already, so a
crash mid-processing just leaves an item `pending`/`retrying`, picked up
normally next pass. This slice adds one regression test proving the
migration-crash-safety claim directly (kill a `Database()` open partway
through applying migrations, confirm a fresh open resumes and completes
correctly) rather than leaving it as an untested assertion.

**2. Bounded Link diagnostic log, metadata only.** No Link operational log
exists today beyond whatever `logging.basicConfig(level=logging.INFO)`
sends to stderr — ephemeral, unbounded (retention is entirely the process
supervisor's problem), and gone the moment a terminal's scrollback rotates
or the service manager's own log rotation fires. A SysOp investigating "why
did sync with peer X stop working three days ago" has nothing durable to
look at. Deliberately **not** a general application-logging overhaul — the
existing `moderation_log` table is already this project's precedent for a
structured, DB-backed log, and the new one is explicitly its bounded,
non-permanent counterpart: a `link_diagnostic_log` table (`id`, `level`,
`logger_name`, `message`, `created_at`), populated by a small `logging.
Handler` subclass attached to the `netbbs.link` logger namespace at startup
(catching every existing `_logger.warning`/`.error` call already scattered
across `netbbs.link.sync`/`.transport`/`.reliable_nodes` via ordinary logger
propagation — no per-call-site instrumentation needed) at `WARNING` level
and above only; routine `INFO`-level chatter stays stderr-only, ephemeral,
exactly as today. Audited every existing call site this handler will now
capture (§13.9's own audit-before-design habit, applied here to *existing*
log statements rather than a new feature): every one is already about
protocol/dial/sync *events* — a URL, a fingerprint, an exception message —
never a Link message's decrypted body, a board post's content, or any other
user-authored payload. "Metadata only, never content" is therefore a
property of which fourteen call sites happen to exist today, not a new
filter this handler has to enforce — worth re-checking whenever a future
Link module adds a new `_logger` call inside this namespace.

Both `LinkConfig.diagnostic_log_max_age_days` (default 30) and
`diagnostic_log_max_rows` (default 5,000) bound it — the handler prunes
against both on every write, cheap at this log's realistic warning-only
volume. Browsable via a new `[D]iagnostic log` SysOp screen under `[S]ystem`
(alongside `[L]ink status`/`[O]utbox`/`[R]epair carried posts`, same
`link_context is not None`-gated visibility), the same paginated-picker
shape `[O]utbox` already uses.

**3. Link wire-protocol version compatibility.** A real, confirmed gap, not
a hypothetical: every canonical event envelope already carries `netbbs_
protocol` (`build_envelope`, `NETBBS_PROTOCOL_VERSION = 1`, round 27) — but
grep confirms nothing anywhere ever reads it back on receipt. A future
protocol revision bumping this field would today be silently ignored by
`handle_hello`/`handle_events`, which would then either crash on an
unfamiliar payload shape with a confusing low-level error, or — worse —
successfully parse a subset of fields that happen to still match and
silently misinterpret the rest. `netbbs.link.protocol` gains one shared
check, applied once per envelope at the single point `handle_events`
already extracts `object_type` before dispatch (covering all nine event
types from one call site, not nine), and separately against the hello
bundle's own embedded transitions/descriptor envelopes in `handle_hello` —
rejecting a `netbbs_protocol` that doesn't exactly equal this build's own
`NETBBS_PROTOCOL_VERSION` with a clear `LinkProtocolError` naming both
versions, never a raw parse failure. "Exactly equal," not a supported range
— there is no forward/backward-compatibility promise to honor yet, since
version 1 is the only version that has ever existed; the point of this
slice is having a real, tested gate *before* a version 2 ever needs one, not
guessing at compatibility rules for a wire change nobody has designed.

The **database** half of "protocol/database upgrade and rollback
compatibility" turns out to already be done, confirmed by reading `netbbs.
selfupdate`'s own module docstring rather than assumed: round 82/95/96
already snapshot the database before applying an update's migration and
roll back to that snapshot if the newly started version fails to come up
cleanly. That same docstring is explicit about the boundary this slice
closes: *"It knows nothing about NetBBS Link protocol/schema compatibility
-- that's explicitly deferred to whenever Phase 3 needs it."* Now is that
moment; the wire-protocol check above is the answer, kept as its own
concern in `netbbs.link.protocol` rather than folded into `netbbs.
selfupdate`, which stays exactly as protocol-agnostic as its own docstring
already declares.

**4. Graceful drain of Link work during shutdown.** `run_link_sync` accepts
an optional `stop_event: asyncio.Event | None`, checked once at the top of
the outer loop (before starting a new pass, not mid-pass — deliberately
simple: passes are normally sub-second, so the value of checking more
granularly inside one is marginal against the complexity of doing so) so a
currently in-flight pass, including whatever HTTP call it's in the middle
of, finishes naturally rather than being aborted mid-request against
whatever peer is on the other end — the one asymmetry ordinary user-session
shutdown didn't have, since that path already warns and waits before
disconnecting anyone. Shutdown sets the event, then `asyncio.wait_for`s the
task against `ShutdownConfig.background_task_drain_seconds` (5s default,
a dedicated timer — not `graceful_delay_seconds`, which answers a
different question, "how long does a *human* get to notice a shutdown
warning before disconnection," and is already fully spent by the time
teardown starts), falling back to a hard `.cancel()` only if that bound is
exceeded (a pass stuck on an unreachable seed's own connect timeout, say).

The loop's own trailing `await asyncio.sleep(interval_seconds)` is
interruptible the same way: `stop_event`-provided callers wait on
`stop_event.wait()` bounded by `asyncio.wait_for(..., timeout=
interval_seconds)` in place of the plain sleep, waking immediately once
shutdown signals rather than waiting out however much of
`sync_interval_seconds` (default 300s) remains — an idle sleep has no
in-flight work to protect, so cutting it short costs nothing the way
interrupting a live HTTP call would. Callers that don't pass a
`stop_event` (`None`, the default) still get the original unconditional
`asyncio.sleep`, unchanged.

`daybreak_task`/`update_check_task`/`reliable_nodes_refresh_task` get a narrower
treatment: none of them talks to a Link peer (`reliable_nodes_refresh_task` fetches
the project's own reliable-nodes roster from `www.netbbs.org`, not a peer's
Link endpoint, and already retries on its own forgiving 24h cadence), so
*graceful* draining — letting current work finish rather than aborting
it — would be solving a problem none of them actually has; all three are
still cancelled immediately, exactly as before. But `update_check_task`/
`reliable_nodes_refresh_task` both reach a blocking `urllib.request.urlopen` call via
`asyncio.to_thread`, and cancelling the *awaiting* coroutine does not stop
that underlying worker thread, which keeps running the blocking call to
completion regardless (Python cannot forcibly abort a thread) — the
shutdown teardown step that then directly `await`s the cancelled task
needs its own ceiling, or an unresponsive fetch there hangs shutdown
independent of anything `link_sync_task` does. All three now share the
same `background_task_drain_seconds` bound `link_sync_task` uses above,
applied to the cancellation-*await* rather than to "let it finish
first" — `daybreak_task` never needed this in practice (a bare
`asyncio.sleep` always cancels promptly), but gets the same treatment for
consistency, at zero real cost.

**Known residual gap (Codex review, PR #228):** the bound above only
covers `netbbs.__main__.run`'s own await of the task. The worker thread
itself is not owned by asyncio and cannot be cancelled — verified by
direct repro, it keeps running (bounded by `urlopen`'s own `timeout=30`,
so tens of seconds, not indefinitely) even after teardown gives up
waiting on it, and Python's interpreter-exit machinery
(`concurrent.futures.thread`'s `atexit` hook) still joins every
outstanding `ThreadPoolExecutor` worker before the process actually
exits, regardless of this bound. A shutdown that looks prompt in the logs
is not yet a guarantee the OS process has actually exited. See
`docs/NetBBS-worklog.md` for the fuller mechanism and candidate fixes;
unresolved as of this writing.

**Closes issue #60.** Every acceptance criterion that issue names is now
either implemented (this slice; §13.4/§13.7/§13.9/§13.10 before it) or an
explicitly deferred, separately-tracked follow-up with its own stated
reasoning (node-wide disk quota and event-retention/purging, §13.9;
per-seed historical/trend health visibility, §13.6) — not a silently
abandoned acceptance criterion.

Implemented.

---

## 14. Testing and interoperability requirements

### 14.1 Deterministic distributed testing

Every implemented Link event family must be exercised through independent node
instances and serialization under applicable scenarios:

- duplicate delivery;
- reordering;
- dropped messages;
- partition and healing;
- restart and state reconstruction;
- malformed or forged events;
- key rotation/revocation;
- convergence after valid resends.

The harness grows with real event families. A generic harness which cannot drive
the real protocol is not sufficient.

**Cross-subsystem end-to-end scenarios (issue #80).** The deterministic
harness above proves protocol/verification logic; it does not, by
itself, prove that a caller-visible guarantee survives the seam between
subsystems (protocol verification, persistence, transport, local-domain
materialization, outbound work tracking, user-visible state). Issue #69
was exactly that: individually correct subsystems, but a self-composed
Link message was never registered where its acknowledgement needed to
find it. `tests/test_link_end_to_end.py` is the named home for this
class of test: a complete real-transport (real `LinkServer`, real
SQLite, real node identities), real-domain-read-path (an ordinary
inbox/board read, not a raw row or `known_event_ids` check) vertical
slice per currently implemented Link product surface — linked boards,
Link mail, (issue #87) linked channels, and (issue #89) remote file
catalogue/chunk transfer — each covering restart-between-stages and
duplicate-delivery. A future Link vertical slice is not complete until it
adds or extends a scenario in that file, the same way it is not complete
without unit tests for its own protocol logic. Tier-2 message routing is
deliberately not in that list — see §10.6 for why it remains deferred
rather than an active future slice.

### 14.2 Real boundaries

Use real:

- SQLite files and independent connections for concurrency and migration tests;
- sockets for transport adapters;
- serialization between separate protocol objects;
- reconstructed objects after restart;
- bounded readiness polling instead of arbitrary sleeps.

Mocks may isolate failures but do not prove the boundary being claimed.

### 14.3 Prove regression tests

When practical, demonstrate that a new regression test fails without the fix.
A test which passes both before and after the supposed fix has not proved the
bug.

Scripted terminal tests must fail fast on input exhaustion and confirm they
reached the intended path after menu or signature changes.

### 14.4 External validation

Automated tests cannot prove visual behavior or third-party interoperability.
Before calling affected functionality production ready, test as applicable with:

- a real OpenSSH client;
- real Telnet terminals;
- SyncTERM/lrzsz or another external Zmodem implementation;
- a real browser/xterm.js session;
- resize, color, CP437 art, editor, bell, and echo behavior;
- long-running operation across midnight and DST changes;
- update, restart, backup, and restore on NetBSD.

### 14.5 Canonical format compatibility vectors

Any change to the canonicalization rule (§7.2) must update
`tests/fixtures/link_canonical_vectors.json` and keep
`tests/test_link_canonical_vectors.py` passing. A vector's canonical bytes or
content ID may only change alongside a deliberate, documented
canonicalization change — never as the side effect of an unrelated
refactor.

---

## 15. Roadmap and phase boundaries

### Phase 1 — Foundation — complete

- modular runtime and SQLite storage;
- node/user identity foundations;
- password and keypair login;
- Telnet, SSH, and web transports;
- ANSI rendering and input plumbing;
- level/permission foundations;
- local boards, file areas, and chat;
- local blocklist foundation.

### Phase 2 — Complete standalone BBS — complete

- local moderation and approval workflows;
- maintenance/expiry;
- user directory, profiles, and finger-style lookup;
- channel visibility, invitations, membership, and moderation;
- local private chat, presence, aliases, and completion;
- SysOp administration and node controls;
- TUI/screen-buffer foundations;
- ANSI and prose editors.

### Post-Phase-2 local additions — substantially complete

- local Communities and Community-scoped authority;
- identity attestation and gates;
- local asynchronous mail;
- self-update foundations and scheduled checks;
- registration-mode and account-lifecycle refinements.

### Phase 3 — Link connectivity and asynchronous services — active

Implemented or substantially working:

- root/operational node-key lifecycle;
- canonical event bytes and signed transition events;
- authenticated hello and endpoint descriptors;
- real HTTP+JSON transport and node startup integration;
- persistent peer and event state;
- foreground/background database lanes;
- configured seeds, live seed refresh, peer-list exchange, and candidate
  fallback;
- deterministic multi-node fault harness;
- linked-board genesis, posts, self-authored edits, and origin transfer/
  orphan/fork behavior; local materialization both of the board shell and of
  received posts/edits (§9.3, issue #73, closed);
- tier-1 Link messages with accepted/bounced delivery state;
- reliability scoring, relay consent, automatic relay selection, and bounded
  relay mailboxes for outgoing-only recipients;
- issue #60's operational controls and recovery model: backup/restore
  (§13.4, §13.10, issue #75, closed), outbound work items/retry/dead-letter
  for Link mail (§13.7), bounded quotas (§13.9), and startup integrity
  checking, diagnostic log retention, protocol/database upgrade
  compatibility, and graceful Link drain on shutdown (§13.11) — issue #60 is
  closed.
- authenticated inventory/pull-based catch-up and multi-hop relay across
  boards, channels, and file-area catalogues, including empty-inventory
  discovery and responder/freshness/replay binding (§8.8, issues
  #85/#94/#106/#124).
- correctness-preserving `key_transition` retention, and the chain-
  idempotency fix that made any retention provable (§8.9, issue #86,
  closed) — board-scoped types remain intentionally unbounded.
- linked channels — genesis, promotion, materialization, and message
  propagation (§9.6, issue #87, closed); origin succession reused by
  reference only, not built, and moderator governance out of scope
  (Phase 6).
- board closure, origin-authorized moderator post edits, and tombstones
  (§9.5, issue #88, closed); linked-board moderator grants/revocations
  remain out of scope.
- remote file area catalogue exchange and on-demand, resumable, deduplicated
  chunk transfer (§11, issue #89, closed); file-area origin succession
  remains out of scope.
- linked-channel messages wired into the live interactive chat send path
  (issue #91, closed) — closes the gap issue #87 left open.
- interactive browse/fetch UI for remote file catalogues: a `/remote`
  command reachable from file areas, both paginated and empty (§11,
  issue #92, closed) — closes the gap issue #89 left open.
- inventory/pull catch-up extended to file-area catalogues (§11.4,
  issue #93, closed) — closes the gap issue #89 left open; content
  bytes still require an explicit fetch, only catalogue metadata is
  recoverable this way.

Operational validation continuing independently of the development cycle:

- broader real-world multi-node deployment validation (issue #83).

### Phase 3 stabilization status and Phase 4 transition

Phase 3 contains enough tested federation behavior to support Phase 4
implementation. Issue #83's sustained dogfood continues as an independent
operational-validation track: its findings remain roadmap evidence and produce
focused fixes, but its calendar duration no longer blocks the development
cycle. Issue #71's independent non-Python interoperability proof is explicitly
deprioritized and remains open as deferred validation rather than a Phase 4
dependency.

This decision advances trust/reputation implementation only. Phase 5
(real-time Link chat), Phase 6 (advanced governance/Link Communities), and
Phase 7 (doors) still require their own explicit sequencing decision.

The Phase 3 validation record is:

- every currently implemented Link product vertical (linked boards,
  linked channels, remote file areas, and Link mail) has at least one
  end-to-end regression test that exercises the real
  sender/receiver/acknowledgement or sender/receiver/materialization
  boundary across a restart, not only isolated unit coverage (issue #80);
- offline/missed-event catch-up exists and demonstrably converges after a
  partition, not only live delivery during an already-connected pass
  (§8.8, issues #85/#94, closed — including discovery from an empty
  inventory when the resource origin is independently known);
- retained event/dedup state has a correctness-preserving retention policy:
  purging the fast dedup cache must not make an old control event
  re-applicable, nor let suppressed or deleted content reappear (§8.9,
  issue #86, closed — `key_transition` alone is purged; every board-scoped
  type is provably still needed and stays unbounded, not silently deferred);
- issue #60's operational controls have been *rehearsed*, not only
  implemented: backup/restore and an upgrade/rollback have each been
  exercised against a real running node at least once beyond their original
  implementation test;
- a sustained real-world multi-node dogfood deployment (issue #83) is ongoing
  independently, with findings converted into issues or worklog invariants
  rather than left as a diary;
- the README, this design document, and the worklog agree on Phase 3's
  actual boundary, and a newcomer can install and run a node from a
  documented path (issues #76, #82);
- known protocol correctness issue #70 is closed. Issue #71's independent
  implementation is deferred: the Python reference implementation and checked
  canonical vectors remain the Link-v1 compatibility authority for now;
  external implementation interoperability remains explicitly unclaimed, and
  every wire change still requires versioning and vector updates.

Advancing development does not imply public federation. Phase 4 is now active
and remains the public-readiness security gate. The issue #55 threat model is
specified in §12; its persistence, protocol, enforcement, UI, and validation
must ship before any public/untrusted federation claim.

### Phase 4 — Trust, reputation, and public readiness — active

After #126, Phase 4 deliberately pauses for a bounded product-track interleave
from issue #83's real-user dogfood feedback before foundation issue #127:

- direct-chat discoverability, single rendering, and field color (#134) —
  implemented: the pinned status row retains `/close` with a compact narrow-
  width form, submitted input is cleared before its committed rendering, and
  identity/message spans are independently sanitized and colored;
- safe line-mode composition and review-before-commit (#133) — implemented:
  the shared line buffer can list/insert/replace/delete submitted lines, and
  local mail, Link mail, and new posts share an explicit editable review state
  before persistence or dispatch;
- truthful single-key yes/no confirmations with Enter defaults (#135) —
  implemented through one shared structured-key primitive without weakening
  generic menu hotkeys; invalid keys retry and accepted choices end their row;
- current-build visual/capability verification plus bounded semantic-color
  polish on named mature surfaces (#136) — implemented: caller and SysOp Who
  share one picker palette; Who, mail, vCards, Last sessions, profile fields,
  picker feedback, and welcome-banner administration distinguish labels,
  values, metadata, success, and failure through shared theme roles; colored
  narrow output is truncated by visible width rather than raw ANSI length.
  The default web login banner visibly exercises truecolor while the
  256-color rendering remains equivalent and readable. Profile and banner-
  preview diagnostics state the transport's detected capability or limitation;
  a custom SysOp banner explicitly bypasses the generated showcase. Both
  Telnet's and SSH's initial banners are shown before capability negotiation
  completes -- Telnet's can precede NEW-ENVIRON, and SSH's own pre-auth
  banner (asyncssh's `send_auth_banner`, sent from `begin_auth` before any
  session channel, and therefore any forwarded environment, exists at all)
  has no client capability to read yet either. Telnet's pre-login banner
  renders the same welcome-banner content at the safe 256-color depth; the
  later profile diagnostic reports the real, negotiated result once
  available. SSH's pre-auth banner (issue #203, dogfood report) sends the
  same content as plain text with every ANSI/VT100 escape sequence
  stripped instead -- `SSH_MSG_USERAUTH_BANNER` is shown during
  authentication itself, and real clients (PuTTY confirmed) commonly route
  it through a display path that never runs an ANSI parser over it at
  all, dumping literal escape bytes rather than color regardless of depth
  chosen; no color depth fixes a client that never interprets escapes at
  this stage. SSH's own *post-auth* welcome screen is unaffected and
  keeps full negotiated color, same as Telnet/web. SSH's pre-auth banner
  is shown regardless of authentication outcome, since it is the only
  screen SSH ever gets before the protocol-level handshake either
  succeeds into the authenticated session or fails.

- sectioned, paginated create/edit screens (dogfood report: the main menu's
  grouped, multi-column layout and the Profile/Board/Area/Channel screens'
  own flat field lists read as wildly different levels of polish) —
  implemented: `edit_resource_draft`'s `FieldSpec.section` groups a screen's
  fields under bold uppercase headings in both the value list and the
  hotkey/menu row, opt-in per screen (Profile, Board, File area, Channel;
  every other screen renders byte-for-byte as before). A dense, sectioned
  screen that still doesn't fit the caller's terminal even at its most
  compact menu tier paginates by section — `Page Up`/`Page Down` cycle
  between them, wrapping at either end, while every field's own hotkey
  keeps working regardless of which page is showing (jumping straight to
  it, switching pages to match) and `[S]ave`/`[B]ack` stay reachable from
  every page. An unsectioned screen has no natural page boundary and keeps
  today's behavior unchanged if it doesn't fit. Profile (14 fields across 4
  sections, plus a bio-preview/transport-diagnostic preamble) is the first
  real screen dense enough to exercise pagination in practice.

This interleave does not change Phase 4's security dependencies or public-
readiness gate. It applies the standing cadence between meaningful foundation
work and complete user-visible slices; #127 resumes after this batch.

- formal threat model from issue #55 — specified in §12;
- persisted local trust inputs, projections, probation, and policy evaluation
  (issue #126) — implemented: separate anchors/reporters/domains, node/user
  subjects, dimension-scoped evidence and vouches, transactional state/audit,
  startup reconciliation, recovery hold, and bounded inactive retention;
- signed trust-signal/vouch subscriptions and evidence verification (issue
  #127) — implemented with explicit bounded pulls, immutable carrier storage,
  issuer verification, replay/freshness checks, and verified digest evidence;
- enforcement across Link transport, sync, relay, content, and users (issue
  #128) — implemented at pre-persistence admission, outbound selection, and
  read-time materialization/display boundaries with stable public reason codes;
- SysOp explanation, overrides, and recovery workflows (issue #129) —
  implemented in the shared SysOp System menu: configuration, effective-state
  explanations, mandatory-reason overrides, recovery, audit history, and
  visibly flagged/confirmed category-scoped sole-authority exceptions;
- remote attestation authority and local acceptance policy (issue #130) —
  implemented with per-attribute opt-in, signed/revocable records, separate
  authority scopes, fail-closed local projections, resource gates, and
  reasoned SysOp explanation/override workflows;
- adversarial distributed validation and the public-readiness gate (issue
  #131) — automated §12.10 evidence is tracked in
  `docs/NetBBS-phase4-readiness.md`; real-node manual recovery,
  independently administered multi-node validation, and sustained private
  dogfood remain pending, so the issue and public-readiness gate remain open.

No public/untrusted federation claim precedes this phase.

### Phase 5 — Real-time Link chat

- Noise transport using node transport keys, with the direct-session and first
  linked-channel vertical specified in §8.10 and tracked by issue #148;
- Link-wide typed chat events, presence, and discovery;
- multiple simultaneous channel memberships and unread/background delivery;
- Link-wide live private chat, distinct from asynchronous Link messages;
- decide whether and how trusted recent scrollback is offered to joining nodes.

### Phase 6 — Advanced Link governance and Link Communities

- linked-channel signed membership/topic governance and origin succession;
- Link-blanket moderator grants and authorized moderation events;
- advanced creation, closure, and lifecycle surfaces;
- Link Communities and signed Community membership/carry changes;
- curated governance audit board and live activity feed.

### Phase 7 — Doors and legacy compatibility — first vertical complete

Implemented (issue #172, closed — supersedes #63 and #167, both closed;
full design record on those two issues' own comment threads, summarized
here since #172 is self-contained):

- subprocess isolation under the same OS user as the main NetBBS process
  — no dedicated door-runner user, no privilege-drop helper, no root
  requirement (deliberately chosen: operational frictionlessness for a
  SysOp outweighs the defense-in-depth a dedicated-user model would buy);
  `resource.setrlimit()` (CPU, memory, process count) set before exec,
  plus an async wall-time watchdog and unconditional reap via the owning
  async task on every exit path (crash/timeout/normal exit/disconnect),
  matching this codebase's standing "creator cancels, gathers, retrieves
  failures" convention;
- a versioned, deliberately minimal v1 API, drop-file-shaped rather than
  a live protocol: static session metadata (handle, stable numeric user
  ID, terminal width/height, color-depth capability, node name) written
  before spawn; stdio is pure raw passthrough for the session's
  duration, with no framing or control messages interleaved; no live
  terminal-resize propagation (matches every classic door's own static
  80x24-era assumption); exit code is the only completion signal;
- door output (stdout) is trusted and relayed unmodified, like a SysOp's
  own welcome-banner file, not run through the chat/post sanitizer —
  NetBBS provides the interface and best-effort abuse prevention within
  its own infrastructure, but the SysOp who chooses to run a given door
  is the one vouching for it, the same posture Phase 4 identity
  attestation already takes;
- SysOp registration (path, name, description) and attachment to a
  board/community, reusing the existing file-area-style permission-level
  gating; caller-facing launch and interactive play across Telnet, SSH,
  and web; door-session start/end audit-logged through the existing
  moderation/audit-log mechanism — no new subsystem;
- two real bundled doors ship as installed package data
  (`netbbs.doors.bundled`, not loose example files), proving the
  pipeline end to end: Retro Trivia, a small demo, and Voidrunner, a
  persistent Elite/Trade-Wars-style space-trading and exploration game
  that grew substantially past its own proof-of-concept scope across
  several post-launch feature and hardening rounds (economy, missions,
  combat, a futures exchange, crew, faction reputation, notoriety/patrol
  encounters, ship progression, retirement/New Game+, and a systemic
  fix for a box-alignment overflow bug that turned out to affect nearly
  every screen in its tactical HUD — v5.4.0 release notes carry the full
  list, not repeated here).

Explicitly out of scope for this vertical, not implemented:

- DOSBox/dosemu compatibility — a later adapter on top of this same
  capability set, deferred by #172's own scope boundary, not merely
  unscheduled;
- multiplayer or persistent cross-session door state — single-player,
  session-scoped only, matching issue #63's own recommendation;
- any door capability beyond the raw-terminal-I/O metadata handshake
  above — deliberately minimal by design, add only what a real door
  actually needs;
- UI-only conversation/message-threading refinements — an item from
  this phase's original planning stub, never folded into #172's own
  scope and not otherwise picked up; still open, unscoped.

`RLIMIT_CPU`/`RLIMIT_AS`/`RLIMIT_NPROC` are all set in the forked child
before exec (`src/netbbs/doors/runtime.py`), matching the locked design's
CPU/memory/process-count ceilings; the wall-time watchdog, crash
reporting, and cleanup-on-disconnect each have a real regression test
(`tests/test_doors_runtime.py`). Not yet independently verified: a
dedicated adversarial test that actually exercises a door hitting the
CPU/memory/process-count ceilings themselves (as opposed to the
watchdog's own wall-time path) — those three `setrlimit` calls are
implemented but currently rely on the OS enforcing them correctly, not
on a test proving it. This design explicitly does not attempt
filesystem/network isolation regardless.

---

## 16. Open design decisions

GitHub issues are authoritative and may evolve beyond this summary.

### Issue #11 — canonical Link format

§7.2/§7.3/§7.4 now state the complete rule: canonical byte encoding (sorted
keys, recursive NFC, compact separators), the safe-integer bound, duplicate-
key wire rejection, omitted-versus-null field semantics, mandatory
object-type domain separation, event-identity distinctness (a nonce for
immutable creation events; `previous_event_id` chains, never `created_at`,
for per-object chains), `(home_node_fingerprint, local_user_id)` as a
node-vouched author's globally-scoped identity, and golden test vectors
(`tests/fixtures/link_canonical_vectors.json`).

Still open: this specification and its vectors exist only as this
codebase's own Python implementation plus one fixture file. No independent,
non-Python implementation has yet exercised the vectors to prove real
cross-language interoperability, and the rule is not yet published as an
external protocol document outside this repository. Closing that gap is
implementation/publication follow-up, not a further design decision.

### Issue #56 — unread, follows, activity, and search

§6.6 now states the complete design: cursor-based read/unread state for
boards, file areas, and channels (mail already had this); replies/mentions
derived from existing `parent_post_id`/message-body fields with no new
schema; a follow/favourite table independent of channel membership and node
carry; a `[N]ew scan` activity surface covering every accessible resource,
not only followed ones, with a direct jump to the first unread item; local
FTS5-backed search scoped to this node's own carried content, explicitly
never broadcast over Link; and a zero-backfill migration story (existing
users' read cursors start empty; first post-upgrade visit sets the
baseline).

Implemented: the read-cursor table (`netbbs.activity`), the follow table, and
the `[N]ew scan` main-menu screen, wired into board/file-area viewing and
channel scrollback replay. Verified against a real Telnet session, not just
scripted tests.

Also implemented: local FTS5-backed search (`netbbs.search`) over board
posts, files, and channel scrollback, synced from every write path, gated by
the exact same visibility rules browsing already enforces, and surfaced as a
new `[F]ind` main-menu entry that jumps straight to a selected hit. FTS5
availability, this round's stated blocker, was resolved by tracing pkgsrc's
actual build chain rather than empirical access to a NetBSD box: `lang/
python312` buildlinks against `databases/sqlite3`, whose own Makefile passes
`--fts5` unconditionally, so the target Python build should always have it —
and a build that doesn't fails the migration loudly rather than silently
disabling search.

Issue #56 is fully implemented; all four §6.6 subsections have shipped.

### Issue #72 — node-local arrival order for unread state — closed

§6.6's "Node-local arrival order for carried content" subsection now
states the complete design: `user_read_cursors.last_seen_arrival_id`,
sourced from `posts`/`files`' own rowid rather than authored
`created_at`, with existing cursors backfilled on upgrade. The one
accepted, documented scope boundary: jump-to-first-unread still uses
the `created_at`-based cursor and may not navigate precisely to an
out-of-order arrival, even though unread counting now correctly flags
it.

### Issue #78 — decompose LinkNode protocol state — closed

The engineering record's "LinkNode internal state organization" entry
(§9) is the design pass this issue asked for: which of `LinkNode`'s
eleven flat fields belong together (`PeerDirectory`, `BoardEventState`,
`BoardLifecycleState`, `RelayState`), and which stay directly on the
façade (`identity`, `known_event_ids`, `events`, as the shared
substrate every object type uses). Every external consumer
(`netbbs.link.store`/`.sync`/`.transport`/`.relay_selection`,
`netbbs.net.admin_flow`, and their tests) still reads the old flat
attribute names unchanged, via backward-compatible properties over the
same live dicts -- zero test changes, zero wire/serialized-shape
changes. A future state family (inventory/pull, linked-channel
lifecycle) should follow the same shape: its own small dataclass with
narrow methods, not a further flat field.

### Issue #80 — end-to-end regression tests for cross-subsystem Link orchestration — closed

§14.1's "Cross-subsystem end-to-end scenarios" subsection now states
the complete design: `tests/test_link_end_to_end.py`, a real-transport,
real-domain-read-path vertical slice per implemented Link product
surface. It now covers linked boards, linked channels, remote file
catalogue/fetch, and Link mail, with restart and duplicate-delivery
coverage where the surface has persisted state. The mail vertical also
covers a dead-letter -> replay -> real-redelivery cycle end to end. Confirmed the consolidated
mail scenario (and its restart variant) would fail on the pre-fix
issue #69 implementation by temporarily reverting the fix and observing
both fail, then restoring it. Future Link vertical slices extend this
file before being considered complete.

### Issue #81 — supported platform tiers — closed

§2.1's "Platform support tiers" subsection now states the complete
policy: NetBSD (Tier 1, primary, with dependencies preferably supplied
by pkgsrc), mainstream Linux/systemd
(Tier 2, supported), other POSIX systems (Tier 3, best-effort), and
Windows (development-only, no production-semantics promise). Auditing
every existing `sys.platform`/`os.name` branch (`netbbs.net.
local_terminal`, `netbbs.backup._process_is_running`,
`netbbs.__main__`'s signal-handler setup) found the codebase already
drew exactly this line in practice, each in its own narrow, already-
isolated function — this closes the gap between that existing practice
and an explicit, written contributor-facing policy, rather than
requiring code changes.

### Issue #82 — operator-ready installation and release path — closed

`docs/NetBBS-operator-guide.md` is the complete operator lifecycle:
install (a real, tested non-editable wheel build/install with no
source-checkout dependency, now sourced only from official GitHub
releases), first-SysOp bootstrap via the existing `netbbs.admin`
CLI, running under systemd/rc.d (`examples/netbbs.service`/`netbbs.rc`,
the rc.d script since confirmed working on real NetBSD hardware,
including its `LD_LIBRARY_PATH` handling for the pkgsrc-vs-base OpenSSL
runtime-linking gap documented in the worklog §10), persistent
state paths, backup/restore (linking the existing disaster-recovery
drill), upgrading, version/schema compatibility, and uninstalling
without losing data. `python -m netbbs --version` (issue #82) prints
the release version and expected schema number together. Documented,
not implemented: `netbbs.selfupdate`'s existing download/snapshot/
rollback plumbing has no wired apply-and-restart command yet — a
deliberate prior deferral (that module's own docstrings), not a gap
this issue asked to close; the package-manager upgrade path is what's
actually supported today.

### Issue #74 — FTS index integrity checks and rebuild tooling — closed

§6.6's "Integrity checking and rebuild" subsection now states the
complete design: `netbbs.search.check_index_integrity`/`rebuild_indexes`,
a standalone `python -m netbbs.search check|rebuild` command, and the
explicit decision that startup does not run this check automatically
(unlike `Database.check_integrity`). Reports drift by id only, never
indexed content, for all three FTS tables.

### Issue #60 — production operations — closed

Implemented across four slices, all merged: backup/restore (§13.4,
`netbbs.backup`, verified against a real running node including a
create-wipe-restore round trip and the live-lock restore refusal); outbound
work items/retry/dead-letter (§13.7, `netbbs.link.work_items`, scoped to
Link mail delivery and acknowledgement delivery specifically — not gossip or
relay maintenance, which don't fit the same shape — wired into
`netbbs.link.mail`/`netbbs.link.sync` and surfaced as an `[O]utbox` SysOp
screen); bounded quotas (§13.9); and startup integrity checking, diagnostic
log retention, protocol/database upgrade compatibility, and graceful Link
drain on shutdown (§13.11). Staged/validated restore (§13.10) shipped
separately as issue #75.

Rehearsing these controls against a real long-running node (not just their
original implementation tests) is tracked by the Phase 3 stabilization gate
above, not by this issue.

### Issue #85 — inventory/pull-based catch-up and multi-hop relay

§8.8 now states the complete design: a signed `InventoryRequest` bundle
(not a canonical event, matching `PeerListMessage`'s own precedent), a new
`POST {LINK_PATH_PREFIX}/inventory/{fingerprint}` route whose response
reuses the exact `push_events` raw-event-list wire shape, a responder-side
diff query unioning three sources (self-originated, locally-authored on any
carried board, and peer-received) so it is genuinely multi-hop, and a
nullable `board_id` column added to `link_events` to make that query cheap.
Bounded by the existing `_MAX_EVENTS_PER_REQUEST`/`max_carried_boards`
quotas, not new numbers.

**One necessary correctness fix, not zero protocol changes**, discovered
while implementing: `handle_events`'s board-scoped branches previously
required the wire-level sender to equal the content's own claimed
origin/author, which made a genuinely relayed event structurally
unverifiable (the relay is a different node than the author). Fixed by
resolving each branch's signing key against the content's own claimed
origin/author instead, gated on that fingerprint independently already
being a completed peer — preserving the same "never accept from a stranger"
property while correctly relocating which fingerprint it applies to. See
§8.8's own "real, worth-stating limitation" note for what this does and
does not enable.

Issue #94 subsequently widened responder enumeration to resources absent
from the request, so an empty inventory can discover a wholly novel board,
channel, or file-area catalogue through a carrier. The signed requester
authentication from issue #106 and destination/freshness/replay binding
from issue #124 are now part of that disclosure boundary; §8.8 is the
normative current request format.

Explicitly excludes retention/purging (issue #86, sequenced after this one)
and Link messages (already point-to-point by design, §10, untouched by the
`handle_events` fix above).

### Issue #86 — event/dedup retention

§8.9 now states the complete design: the chain-idempotency gap was in
`board_origin_transfer_offer`/`_accepted`, the only two board-scoped types
whose idempotency depended solely on the fast `known_event_ids` cache
rather than a self-heal check against their own authoritative state
(`pending_offer`/`board_lifecycle_head`) — fixed with the same self-heal
shape `key_transition`/`board_post_edit` already use. Tracing what else
depends on each object type's `link_events` row surviving (restart
reconstruction, and issue #85's own inventory diff) found only
`key_transition` genuinely redundant with an already-durable separate
source (`link_peers.transitions_json`); everything board-scoped stays
unbounded in this issue, stated explicitly rather than silently assumed
safe. `netbbs.link.store.purge_expired_key_transitions` purges on write
(90-day fixed window), the same shape `LinkDiagnosticLogHandler.emit`
already established for `link_diagnostic_log`.

### Issue #90 — tier-2 Link message routing scope — deferred

§10.6 now states the complete answer: distinct from the unrelated,
already-decided `tier2_personal_key` non-goal (§10.2, a hard architectural
blocker); this is a recipient-*reachability* question with no equivalent
blocker, just not built. Confirmed that issue #85's inventory/relay work
does *not* provide tier-2 reachability — it relays content whose author
must already be a directly-known peer, never bootstraps a new peer's
identity. A real design exists if this is ever picked up (a relayed,
self-certifying `HelloMessage` bundle exchange, reusing the same
self-authentication property §12 already relies on), but is deliberately
deferred rather than scoped as active work — no architectural blocker,
just not needed to unblock or validate current Phase 3 work.

### Issue #87 — linked channels — closed

§9.6 states the complete design and is now fully implemented:
`channel_genesis`/`channel_message` event types mirroring `board_genesis`/
`board_post` minus the fields that don't apply (no edit chain — channel
messages have no local edit concept at all; no `default_min_write_level`/
`_moderated`/`_max_post_age_days` equivalents — `Channel` has none of those
settings to recommend). Origin succession is reused by reference (§9.4's
model applies unchanged, if ever needed) rather than a new
`channel_origin_transfer_offer`/`_accepted` pair built in this issue —
genesis, promotion, materialization, and message propagation are the
actual scope, matching what actually shipped. `netbbs.link.channels`
mirrors `netbbs.link.boards` closely; `ChannelEventState` (genesis only, no
edit-chain half) mirrors `BoardEventState`; `handle_events` gained
`channel_genesis`/`channel_message` branches with issue #85's multi-hop
verification model from day one (never had the older restriction to begin
with). Issue #85's inventory mechanism (`InventoryRequest.channels`,
`channel_event_diff`) and issue #86's restart reconstruction both extend
to channels the same way they already cover boards.

Carry/materialization follows §9.3's exact shape, with one real, stated
consequence of reusing the existing bounded scrollback rather than
inventing unbounded storage for channel content: a materialized linked
message is subject to the same trim a local one already is, and a
self-originated message trimmed before any sync pass pushes it is simply
never propagated — bounded and honestly scoped, not a silent surprise,
since the identical bound already governs every channel's own local
history today. `channel_messages.link_content_id` is a new column solving
a real gap found while implementing: unlike `posts.post_id`,
`channel_messages.id` is a plain autoincrement with no existing
content-addressed column to key idempotent materialization off of.

**Not done in this issue, closed by issue #91:** wiring `queue_channel_
message_if_linked` into `netbbs.net.chat_flow`'s live interactive send
path was deferred here (`chat_flow.py`'s message-send code had no existing
`link_context` threading at all, unlike `login_flow.py`'s board-post path)
and completed separately — see issue #91's own §16 entry.

### Issue #88 — board closure, moderator edits, tombstones — closed

§9.5 states the complete design and is now fully implemented: `board_closure`
(extends §9.4's own `board_lifecycle_head` chain, terminal), `board_post_
moderator_edit`/`board_post_tombstone` (both extend the same per-post chain
`board_post_edit` already does, origin-signed rather than author-signed, the
latter terminal too). No new authorization primitive — all three verify
against the board's current origin's signing key exactly like `board_origin_
transfer_offer` already does; the two post-scoped types rely on a local
`BoardPermission.EDIT`/`DELETE` check already made on the origin node, before
`netbbs.link.boards.queue_board_post_moderator_edit_if_linked`/`queue_board_
post_tombstone_if_linked` ever builds the event — never a new gossiped grant.

`posts.tombstoned_at` is a plain nullable `ALTER TABLE ADD COLUMN`, not a
`CHECK`-widening rebuild: an earlier migration (post-`root_post_id`) already
found and documented that `posts` cannot safely go through the usual
drop/rebuild pattern, since it is a live *self-referencing* FK parent
(`parent_post_id`/`root_post_id`/`edit_of_post_id` all reference `posts.
post_id`) — the additive-column path sidesteps that risk entirely rather
than re-testing it. `netbbs.boards.posts.tombstone_post` is a new local
function, not a repurposed `delete_post`: it inserts a further
content-addressed revision (placeholder subject/body, `tombstoned_at` set)
rather than removing the row, so the edit chain and any reply's
`parent_post_id` stay intact — `delete_post` itself is unchanged, still
reserved for a still-`'pending'` post's rejection.

**Live UI wiring, stated explicitly:** `board_closure` and `board_post_
moderator_edit` reach real interactive call sites this issue — a `[C]lose`
option on the board admin screen (`netbbs.net.admin_flow`, gated the same way
`[T]ransfer origin` already is: origin only, board not already closed), and
the existing `[E]dit` flow (`netbbs.net.board_flow._edit_existing_post`)
building a moderator-edit event instead of a self-authored one whenever
`edited_by` isn't the post's own author and this node is the board's origin.
`board_post_tombstone` did not have any existing "delete an approved,
already-published post" UI action to extend (the only existing `delete_post`
call site handles pending-post rejection, which never reaches an already-
`board_post`-queued row) — a new `[T]ombstone` option was added alongside
`[E]dit` for exactly this reason, gated on `BoardPermission.DELETE`.

### Issue #89 — remote file catalogue and chunk transfer — closed

§11 states the complete design and is now fully implemented: `file_area_
genesis`/`file_descriptor` mirror `board_genesis`/`board_post` for catalogue
discovery (no content, no edit chain, no origin succession — the same
deliberate deferrals issue #87 already set for channels); chunk transfer
(`netbbs.link.file_transfer`, `FileChunkRequest`/`FileChunkDescriptor`) is
genuinely new — a direct point-to-point pull against the file's own origin,
never gossiped, never passed through `handle_events`. `remote_files` is a
new table, not a row in the real `files` table — that table's own invariant
("bytes always exist before the row does") stays true unconditionally, and
a catalogued-but-not-fetched file is a state `files` was never designed to
represent; `netbbs.files.storage`'s existing content-addressed layout is
reused once a transfer completes and verifies, so a fetched file dedups by
hash automatically, same as a local upload. `transfer_id` is deterministic
(`file_id` + this node's own fingerprint), and `chunk_id` (the chunk's own
sha256) is the exact-dedup key for a resent/duplicate chunk request —
found and fixed a real bug while writing tests: `materialize_carried_file_
descriptor` initially keyed `remote_files.file_id` off the signed *event's*
own `content_id` rather than `payload["file_id"]` (the file's actual local
identity) — two different hashes for the same object, the same distinction
`FileDescriptor`'s own docstring already calls out as the reason `file_id`
is carried explicitly in the payload rather than reusing `BoardPost`'s
"the event's content_id already is the object's identity" precedent.

Bounded per §13.5: `max_carried_file_areas`/`max_remote_files_per_area`
(carry-limit errors, tolerated the same way `BoardCarryLimitError` already
is), and `max_concurrent_file_transfers_per_peer` on the serving side,
tracked in memory only (never persisted — serving one chunk is otherwise
fully stateless). Chunk transfer is deliberately not folded into issue #60's
work-item/DLQ abstraction, the same "not every retry-shaped mechanism fits"
reasoning §13.7 already documents — it already has a natural resumable-by-
construction terminal state.

**Explicitly out of scope at the time, tracked separately and since
closed:** file-area origin succession remains untracked (no issue yet);
inventory/pull catch-up (§8.8) extended to file areas was deferred here and
closed by issue #93 (§11.4); a live SysOp/user TUI action to browse a
remote area's catalogue or trigger a fetch was deferred here and closed by
issue #92.

### Issue #91 — wire linked-channel messages into the live chat send path — closed

Closes the gap issue #87's own §16 entry named: `netbbs.net.chat_flow._chat_
loop`'s message-send path now threads an optional `link_context` (mirroring
`netbbs.net.board_flow._show_board`'s own parameter for board posts exactly)
down from `browse_channels`, and calls `netbbs.link.channels.queue_channel_
message_if_linked` right after a self-authored message is locally recorded,
whenever `channel` is Linked. Fire-and-forget, no separate success/failure
message shown to the sender — the same shape `queue_board_post_if_linked`'s
own call site already established; the actual outbound push, and its own
failure handling, lives entirely in `netbbs.link.sync`'s existing background
loop, unchanged by this issue. An unlinked channel, or a session with no
`link_context` at all (Link disabled, or a caller bypassing `netbbs.net.
login_flow.handle_session`'s real wiring), behaves exactly as before —
local chat stays fully usable and Link-unaware.

Minimal threading, no broader `chat_flow` refactor: only `browse_channels`/
`_chat_loop` gained the new parameter, and only the three existing `netbbs.
net.login_flow` call sites (`[N]ew scan`, `[F]ind`, the main channel-browse
menu) needed updating to pass their own already-in-scope `link_context`
through. A real two-node end-to-end test (`tests/test_link_end_to_end.py`)
drives `_chat_loop` itself with a scripted `FakeSession`, not a direct
`queue_channel_message_if_linked` call, proving the interactive send path
specifically, per the issue's own acceptance criterion.

### Issue #92 — interactive browse/fetch UI for remote file catalogues — closed

Closes the UI gap issue #89 left open: `netbbs.net.file_flow._show_area`
gains a `/remote` command (reachable from both the ordinary paginated file
listing and the "has no files yet" fallback prompt, since a Linked area can
have remote catalogue entries with zero *local* uploads of its own),
offered whenever an optional `link_context` is given — threaded down from
`enter_file_area`/`browse_file_areas` the same way `netbbs.net.login_flow`'s
board/channel paths already thread it. `/remote` lists every catalogued
`RemoteFile` for the area via `netbbs.link.files.list_remote_files`
(fetched and not-yet-fetched alike, clearly labeled, so a user can tell
"catalogued and I already have it" from "catalogued and I don't" at a
glance — no separate hidden state); picking an already-fetched entry
reports that and stops, never re-offering a redundant fetch. Picking a
not-yet-fetched entry, after a yes/no confirmation, drives `netbbs.link.
transport.fetch_next_file_chunk` in a loop until the transfer completes,
fails, or the origin turns out unreachable (`dialable_base_urls_for_peer`,
new — chunk transfer is never relayed, so an origin with no advertised
direct address is reported clearly rather than attempted). Success is
reported once the transfer's own existing verification/promotion path
(unchanged by this issue) has already placed the content in the ordinary
local `files` table; the file is then reachable through the pre-existing
`/download` command like any other, no new download path introduced.

No per-file access check inside `/remote` beyond what already gated
entering `_show_area` in the first place (design doc's own "merely knowing
a descriptor exists must not bypass local policy" acceptance criterion) —
a `RemoteFile` carries no independent moderation state of its own the way
a pending local upload does, so the area-level read/age/name-requirement
gate already enforced by whichever picker offered the area is sufficient.

`aiohttp`/`netbbs.link.transport` are imported lazily, inside the one
function that actually dials out (`_fetch_remote_file`) — `netbbs.net.
file_flow` is loaded unconditionally by every node, including one with
`aiohttp` not installed, matching the same lazy-import convention
`netbbs.__main__`'s own Link-server startup already established.

A full interactive-flow regression test (`tests/test_link_end_to_end.py`)
drives `_show_area` itself against a real second node — `/remote` command,
`pick_item` selection, the fetch confirmation prompt — proving browse ->
fetch -> verify/promote -> ordinary `/download` visibility end to end, per
the issue's own acceptance criterion; `tests/test_file_flow_remote.py`
covers the UI-level edge cases that don't need a real second node (no
catalogue entries, an already-fetched entry, a declined fetch, an
unreachable origin).

### Issue #93 — inventory/pull catch-up for linked file-area catalogues — closed

§11.4 states the complete design and is now fully implemented: `Inventory
Request` gains a third `file_areas` key alongside `boards`/`channels`, the
identical shape issue #87 already added for channels; `netbbs.link.store.
file_area_event_diff`/`_all_file_area_events` mirror `board_event_diff`/
`channel_event_diff` exactly, unioning the same three sources (self-
originated genesis, self-authored descriptors, peer-received `link_events`
rows) those functions already established. `_handle_inventory` now shares
one overall `_MAX_EVENTS_PER_REQUEST` budget across all three diffs in
sequence (board, then channel, then file area) rather than three
independent caps.

`link_events` gains a nullable `file_area_id` column, populated at two
call sites rather than one: `netbbs.link.store.save_event`'s generic
dispatch (for `file_area_genesis`, mirroring how `channel_id` is populated
for `channel_genesis`) and `netbbs.link.files.materialize_carried_file_
descriptor`'s own direct insert (for `file_descriptor`, which — like
`board_post`/`channel_message` before it — skips `save_event` entirely).
`sync.py`'s inventory-response step also gained `max_carried_file_areas`/
`max_remote_files_per_area` parameters, threaded through from `__main__.py`
the same way `max_carried_boards`/`max_carried_channels` already are — a
real pre-existing gap, not a cosmetic one: before this issue, an inventory
response could carry an unbounded number of new file areas/descriptors
even though `LinkServer`'s direct-push path already enforced these same
two quotas (§13.9).

No restart-reconstruction changes were needed: issue #89's own `load_link_
node` work already rebuilt `node.file_areas` from both sources this
issue's diff query also reads, and `file_descriptor` has no chain state
beyond `known_event_ids`/`events` to begin with. Proven with the same two-
layer test shape issues #85/#87 established: a deterministic three-node
`ScriptedTransport` test (`tests/test_link_convergence.py`) proving the
protocol-level multi-hop mechanism in isolation, and a real-transport test
(`tests/test_link_end_to_end.py`) proving a node recovers a missed `file_
descriptor` through an intermediary while the original origin's server is
never started again for that stage — genuinely unavailable, not merely
unqueried. Chunk bytes remain outside inventory entirely and unchanged by
this issue, confirmed by both tests: a recovered catalogue entry lands
with `fetched_file_id` still `NULL`.

### Issue #55 — trust and quarantine — design specified

§12 specifies the Phase-4 attacker model, evidence classes, explicit reporter
trust domains, Sybil/weight rules, signal bounds and lifetimes, probation,
quarantine, reversibility, explainability, and required validation. Phase-4
implementation remains separate roadmap work; completing the design issue does
not itself make public federation safe.

### Issue #63 — door isolation

Define process/jail/container boundaries, filesystem/network access, resource
limits, terminal mediation, session capability API, audit, crash cleanup, and
DOS adapter behavior.

**Candidate approach, not yet committed — exposing Link to doors.** Rather than
giving doors raw Link access, mediate it through the session capability API,
sized per interaction latency:

- real-time move exchange rides the future Phase 5 real-time Link chat
  channel;
- turn submission for asynchronous games (chess, TradeWars-style turns) maps
  onto linked-board events on a shared game board, reusing existing
  carry-materialization rather than new plumbing;
- point-to-point in-game mail maps onto tier-1 `link_message`;
- federated high-score lists fit neither primitive cleanly — they are shared,
  mergeable state with concurrent writers, so they need an explicit
  conflict-resolution rule (e.g. monotonic max per player) before they can
  ride on either.

To keep door-facing boards/channels invisible to ordinary users without new
schema, consider gating them with an elevated minimum user level (e.g. 245)
rather than a new visibility flag, since minimum-level is already a resource
gate (§5.1) and sits safely below `SYSOP_LEVEL = 255`. This only works if:

- door processes write under their own capability-scoped service identity
  minted by the session capability API, not the player's own account level;
- board/channel listing queries honor the minimum-level gate, not just entry,
  so gated resources don't appear in listings for users below the threshold;
- the level band used for infrastructure resources (e.g. 240–254) is a named
  constant, so a future SysOp level-preset feature cannot hand that range to a
  real user by accident.

### Issue #165 — MRC gateway scoping

**Goal:** let NetBBS callers reach the existing cross-BBS MRC (Multi Relay
Chat) network without coupling NetBBS's own premium chat model — Noise
XX transport, Link-wide presence, per-message trust-filtered scrollback,
all closed by issue #164 — to a third-party network's pace or ceiling.
Sequenced after #164, which is done (shipped in v5.3.0): this scoping
pass was unblocked the moment that landed.

**Reference protocol, since no formal MRC spec exists:** verified
directly against ENiGMA½ BBS's own reference client/multiplexer
(`core/mrc.js`, `core/servers/chat/mrc_multiplexer.js`), the most
actively maintained modern implementation. MRC is a single central hub
(historically `mrc.bottomlessabyss.net`), not a federated or
peer-to-peer network — every participating BBS is one more client of the
same hub. The wire protocol is a single persistent TCP socket (TLS
optional, separate port), newline-delimited, tilde-separated 7-field
lines: `from_user~from_site~from_room~to_user~to_site~to_room~body~`.
`to_user`/`to_room` double as addressing and as an ad hoc control
channel (`to_user="SERVER"`/`"CLIENT"` carries heartbeat/roster/registration
commands like `IAMHERE`, `USERLIST`, `STATS`, `LOGOFF`, `INFOSYS`/`INFOWEB`/
etc.). The only "handshake" is one unauthenticated line the client sends
on connect — `{boardName}~{clientSoftware}/{os}/{version}` — no password,
token, or signature ties a connection, a user name, or a claimed board
name to anything real; any client can claim to be any board. Name fields
are constrained to ASCII 33–125, 30 chars, with Mystic `|NN` pipe-color
codes stripped; message bodies to ASCII 32–125. There is no history or
backfill concept at all — a message reaches only whatever clients happen
to be connected at the instant it's sent.

This resolves several of the issue's own open questions directly, rather
than leaving them for a later pass: MRC has no identity a gateway could
verify, so nothing about it can ever feed Phase 4's trust/reputation
model, and nothing resembling NetBBS's own trusted-scrollback concept
has an MRC-side equivalent to bridge to.

**Existing precedent this reuses, not reinvents:** `netbbs.link.
realtime_channels.LiveChannelBridge` is the architectural template — one
instance per running node, holding no storage of its own, forwarding
everything through the same `netbbs.chat.hub.ChatHub.broadcast()` a
purely local participant's own message already goes through. Its own
`_handle_channel_message` already renders a remote-authored message with
`author_fingerprint=None` and a descriptive `author_label` (`f"{user_id}
@{fingerprint}"`) — the exact shape an MRC-authored message needs, no new
`channel_messages` column or storage rule required. The outbound hook
point already exists too: `netbbs.net.chat_flow`'s per-message send path
calls `link_context.realtime_bridge.broadcast_local_message_live(channel,
recorded_message)` immediately after `record_message`/`hub.broadcast` —
a sibling MRC bridge attaches at that identical call site, independent
of `link_context` and of whether Link itself is even configured.

**Decision 1 (locked in) — one in-process bridge per running node, no
separate daemon.** Unlike ENiGMA½'s own two-tier design (a per-connection
client process plus a separate local "multiplexer" process fanning
multiple local sessions into one hub connection), NetBBS is already a
single asyncio process per node — there is no per-connection-process
architecture here to multiplex in the first place. One `MrcBridge`
instance, analogous to `LiveChannelBridge`, owns the one outbound hub
socket for the whole node.

**Decision 2 (locked in) — bridging is per-channel, explicit, and off by
default; never automatic for every channel.** MRC rooms are flat,
global, and unauthenticated with no ACL concept at all — "every local
channel bridges by default" would silently leak channel contents onto a
public, unauthenticated network the moment a SysOp enables MRC at all.
A SysOp must name which local channel maps to which MRC room, the same
opt-in shape every other Link-adjacent per-channel setting already uses.

**Decision 3 (locked in) — channels only, never direct/private chat.**
Matches the issue's own framing, and is reinforced by the protocol
itself: MRC's `to_user` targeting carries no real confidentiality (the
hub, or any client willing to lie about its own identity, can see or
spoof it), so gatewaying anything shaped like private chat would be a
false promise of privacy NetBBS itself doesn't need to make.

**Decision 4 (locked in) — inbound content is always rendered as
external/untrusted, and never enters Phase 4 at all.** An inbound MRC
message becomes a `ChannelMessage` with `author_fingerprint=None` and an
`author_label` such as `f"{mrc_user}@{mrc_site} (MRC)"` — visually
distinct from both a local and a genuine Link-originated author label.
It is never passed to `decide_node_action`/quarantine/trust scoring:
there is no cryptographic identity on the MRC side to hang a trust
decision on, so inventing one would be theater, not protection.

**Decision 5 (locked in) — bounded failure containment, matching this
project's existing "bound remotely influenced resources" invariant.**
Reconnect-with-backoff on the one outbound socket (mirroring the retry
behavior every MRC implementation already converges on); a bounded
outbound send queue so a stalled or unreachable hub degrades to that one
bridge going quiet, never blocking local chat delivery or the caller who
just sent a message; malformed or oversized inbound lines are dropped
and logged, never allowed to crash the local channel. Every outbound
field is sanitized to MRC's own documented charset/length limits before
it's sent; inbound Mystic `|NN` pipe-color codes are stripped, never
rendered as NetBBS's own ANSI — matches "sanitize before styling," and
treats them as untrusted input either way.

**Left open for a future implementation pass, deliberately not decided
here:** the exact SysOp-facing configuration screen shape (a new Settings
leaf, or folded into an existing per-channel settings surface); what a
live bridge's connected/disconnected status looks like to a SysOp; and
whether/how a SysOp can disable one misbehaving bridge without touching
MRC node-wide. This scoping pass answers architecture and trust-boundary
questions; it does not itself authorize implementation to begin.

### Issue #194 — trusted scrollback-on-join — closed

**Goal:** decide whether/how a node gets recent scrollback the instant it
live-subscribes to a linked channel. Today it gets presence plus
messages going forward only (§8.10.2's own "does not offer shared recent
scrollback"), a deliberate v1 scope cut, not an oversight — but a real
gap against this feature's own "frictionless" bar.

**Not a permanent-loss problem, and not shaped like issue #168 at all.**
Every channel message on a linked channel is already a signed, durable
`channel_message` event (`queue_channel_message_if_linked`), delivered to
every linked node eventually through the existing inventory/pull-based
catch-up path (issue #85) regardless of live-connection status.
`netbbs.link.sync`'s scheduling loop runs every `sync_interval_seconds`
(five minutes by default), so today a freshly live-subscribed channel
can sit silent for up to that long before the *existing* async path
fills in what was missed. This decision only shrinks that window at
subscribe time — it never touches durability. It also carries none of
issue #168's relay/crypto-fork complexity: `ensure_live_subscription`
(`netbbs.link.realtime_channels`) already always dials the channel's
*origin* node specifically (`channel_origin_fingerprint`), never a third
node, so there is exactly one source of truth to ask, not a multi-hop
question.

**Decision 1 (locked in) — source is the origin's own local scrollback,
sent as a new frame alongside the existing subscribe-time
`presence_snapshot`.** `_handle_subscribe` already sends a channel's live
presence roster the moment a peer subscribes (§8.10.2); a
`scrollback_snapshot` frame is a sibling addition at the identical call
site, sourced from `netbbs.chat.scrollback.get_scrollback` — the same
already-bounded, origin-policy-filtered function the local UI renders
from. A subscriber accepts the frame only from the channel's current
authenticated origin and only when its `request_id` matches a still-pending
subscribe attempt; unsolicited or late snapshots cannot fill a later join.

**Decision 2 (locked in) — bounded the same way every other snapshot
already is, and rendered once, never durably stored on the subscribing
side.** A received `scrollback_snapshot` is ephemeral and render-only —
exactly the same "Live channel messages are ephemeral node-attested
assertions" principle §8.10.2 already states for ordinary live messages.
Writing it into the subscriber's own `channel_messages` was considered
and rejected: the same content already arrives durably, independently,
through the existing async materialization path, and persisting both
would risk duplicate or conflicting rows for one message with no natural
dedup key across the two paths. Snapshot entries therefore carry the signed
event's existing content ID when one exists, letting the join flow suppress
anything already rendered from local materialized history. A caller sees
only the remaining gap once at subscribe time; the durable copy still lands
separately, on its own schedule, as it already does today.

**Decision 3 (locked in) — issue #164's author-trust rule is enforced at
both nodes.** The origin's `get_scrollback` applies its own policy first.
Each entry also carries the author identity the origin derived from the
accepted signed event (or the origin plus local user ID for origin-local
content), so the subscriber independently suppresses authors it marks
`BLOCKED` or `QUARANTINED`. Origin-local display labels are qualified with
the authenticated origin; they must never resolve as same-named local
accounts on the subscriber.

The revised snapshot attribution contract is real-time protocol v2. The Noise
identity payload declares that application version, and incompatible peers are
rejected during the authenticated handshake before either side advertises a
usable live session. The join flow reports this as an explicit upgrade
requirement rather than folding it into generic transient unavailability.
Frame versions remain an inner defensive boundary.
The subscriber reconstructs every authored display label from the attested
`user@node` identity rather than trusting the wire label. Moderation entries
retain their target label for rendering but are authorless system events, so a
target's trust state cannot suppress audit history the target did not author.

`chat/scrollback.py`'s own module docstring — "the separate, harder
question of a newly-joined Link node needing catch-up scrollback from
peers... stays explicitly deferred to whenever Phase 5 starts" — is
updated alongside this decision: Phase 5 has been active for a while now,
and this issue is that decision, not a further deferral of it.

### Issue #200 — War Dialer: async multiplayer BBS-crew door game — closed

**Goal:** decide the design for a second door-game genre alongside
Voidrunner's single-player persistent model — an asynchronous,
play-by-post multiplayer game in the LORD/TradeWars BBS-door lineage,
where actions taken by one player affect another player's persistent
state regardless of whether that player is online, and the target finds
out via a summary on their next login.

**Decision 1 (locked in) — no platform or door-API changes.** §Phase 7
already states doors run with real filesystem access and no enforced
isolation. This game owns its own SQLite database (WAL mode) keyed by
the door API's stable numeric user ID, the same pattern Voidrunner
already uses for its own save state. Cross-player shared state is
therefore the door's own responsibility, not a new NetBBS capability —
issue #63's "single-player, session-scoped only" boundary for the
platform itself is unaffected.

**Decision 2 (locked in) — resolution is synchronous, not tick-based.**
Every action resolves inside the acting player's own live door session.
No cron/daemon, matching the fact that a door process only exists while
someone is logged in. Passive accrual (territory income, Heat decay) is
computed lazily from elapsed wall-clock time whenever next read, the
standard idle-game pattern, rather than requiring a background tick.

**Decision 3 (locked in) — theme is 80s/90s BBS-scene hacker/phreaker
crews, not a generic crime-syndicate reskin.** Rival crews (Legion of
Doom/Masters of Deception energy) fighting over "exchanges" (phone
NPA-NXX prefixes); "Heat" is literal federal attention
(Sundevil-era Secret Service/FBI); this reads as the platform's own
history rather than the genre's default reskin, which every existing
Discord clone already uses.

**Decision 4 (locked in) — Rank is a single, monotonic metric doing
double duty as leaderboard score and PvP bracket gate.** Tiers Newbie/
Wannabe/Script Kiddie/Hacker/Elite/Legend. Never decreasing within a
season was the deciding constraint: a raw balance that could drop would
let a strong player deliberately sandbag into a weaker bracket to prey
on newcomers. Direct PvP (Raid) is gated to your own tier ±1; territory
contests (Root the Exchange) are intentionally *not* gated, since a
district's defense is the controller's committed garrison rather than
their full Rank, keeping territory contestable by newcomers even
against a top-bracket controller.

**Corrected at implementation time:** this decision was originally
recorded as `crew×10 + exchanges_controlled×500 + successful_raids×25 +
successful_jobs×15` — but `crew` and `exchanges_controlled` there meant
*current* holdings, which can both go down (a bust, a rival rooting
your exchange), silently reintroducing the exact sandbagging hole this
decision exists to close. The shipped formula instead sums four
lifetime counters that only ever increment — crew *ever* recruited,
exchanges *ever* successfully taken, successful raids, successful jobs
— `crew_recruited_total×10 + exchanges_taken_total×500 +
successful_raids×25 + successful_jobs×15`
(`netbbs.doors.bundled.war_dialer.rank_score`). Current crew and
current exchange-control remain separate, ordinary fluctuating fields
used only for combat odds and territory defense — never inputs to
Rank.

**Decision 5 (locked in) — seasons (4 weeks), separate from Rank
brackets, because they solve a different problem.** Brackets prevent
moment-to-moment exploitation (a veteran farming a specific weak
player); they do nothing about long-run stagnation, where early players
simply compound the largest crews/territory indefinitely and a
newcomer has no on-ramp. A season boundary resets
Rank/cash/crew/exchange-control, flavored in-fiction as a Fed crackdown
wiping the scene's boards.

**Decision 6 (locked in) — bounded action economy and a self-limiting
risk curve, no separate anti-snowball mechanic.** 15 turns/day on a
rolling 24h window. Heat gain per action (Trade Warez +2, Root Exchange
+8, Raid +10, Run a Job +15) decays −5/real-hour; above 80, each
heat-gaining action rolls a bust chance of `(Heat−80)×2%`, capped ~40%,
costing 25% cash and 20% crew and resetting Heat to 0. Success chance
for both PvE and PvP actions is `attacker_crew / (attacker_crew +
defender_crew)`, clamped to [10%, 90%] so nothing is ever a guaranteed
win or loss. New accounts get 48h Raid immunity, and no attacker may
Raid the same target twice in a row without the target logging in
between.

Explicitly out of scope for v1: procedural exchange generation, a
multi-resource economy, factions/alliances, and an item/weapon shop —
plausible v2 additions once the core loop is proven, not part of this
vertical.

Implemented in full (`src/netbbs/doors/bundled/war_dialer.py`, listed
in `BUNDLED_DOORS` alongside Retro Trivia and Voidrunner): all five
actions with the formulas above, the offline "while you were away"
event summary, lazy season/turn/Heat catch-up on login, the bust
mechanic, and both new-player protections. 33 tests
(`tests/test_war_dialer_domain.py`) cover the domain formulas plus
real-SQLite storage-layer behavior, including a real-threads
concurrency regression proving two simultaneous raids on the same
target row cannot lose an update. Issue #200 is closed; all of its
acceptance criteria are met.

### Issue #168 — real-time relay for Link direct chat

**Goal:** decide between the two structurally different designs the issue
itself poses for live (Noise XX) relay between two mutually-unreachable
(outgoing-only-to-each-other) nodes: double-hop relay-as-participant
(the relay terminates one Noise session per leg and re-encrypts between
them) versus a raw-socket/TCP-level proxy below the Noise layer (the
relay blindly forwards bytes, never touching the handshake or its keys).
Not a newly-discovered gap — the design doc already named this as a
deferred "separate future protocol" (§8.10); this issue is that design
pass.

**Context that made this tractable now, not a decision in itself:** a
second, related idea is in discussion — turning ReLink (the project's
own persistent, internet-reachable test node) into a stable, always-up
default relay so a home SysOp who can't or won't expose a port has a
frictionless path onto the mesh, without that being a hard dependency
(any other willing peer, or a commercial provider for the parallel
managed-DNS idea under #201, works exactly as well — nothing forces
ReLink specifically). That product/infrastructure question is real but
still early and not decided here. What it *does* settle is the async-
relay model's own objection to the raw-proxy design: `relay_selection.py`'s
reliability-ranking machinery exists to route around relays that might
disappear, and a relay explicitly committed to staying up removes the
need to solve general reliability-ranked live-relay discovery before a
v1 can ship. The protocol decision below stands on its own regardless of
who ends up operating such a relay.

**Decision 1 (locked in) — raw-socket/TCP-level proxy, not double-hop.**
The deciding factor: raw-proxy requires **zero changes** to the already-
shipped `LinkRealtimeSession`/Noise XX handshake code. Two directly-
handshaked endpoints run the exact same mutual authentication they'd run
if actually adjacent; the relay is as invisible to Noise as any ordinary
router hop, since it never participates in the Diffie-Hellman exchange
and structurally cannot decrypt anything. That confines all new code to
connection setup (a small rendezvous exchange — "I want to reach
fingerprint X" / "I'm X, waiting"), not the confidentiality-critical
path itself, and it is a well-understood pattern elsewhere (a TURN
server, an SSH jump host, Tailscale's DERP relays), not a novel design.

A real alternative was considered and rejected for now, not dismissed:
double-hop can be built as a *hybrid* where the relay stays a genuine
protocol participant for control-plane frames (subscribe/presence/ping)
it's fine to see, while chat-content frames carry their own additional
encryption hop the relay can't read — giving real per-frame-type abuse
mitigation (raw-proxy can only see bytes/timing, never structure) on top
of content confidentiality. Rejected for v1 because it's solving a
structural-abuse-mitigation problem with no evidence yet that it's
needed, at real cryptographic-design cost paid up front: a second key-
exchange scheme layered inside the double-hop transport, a new session
shape (today's `LinkRealtimeSession` assumes exactly one remote
fingerprint, not a triangulated A-relay-B relationship), and a new frame
family. If frame-level abuse mitigation becomes a real operational
problem later, the hybrid design is the documented answer to revisit —
not re-derived from scratch.

**Trade-off stated plainly:** raw-proxy does not get either design to
zero metadata exposure — the relay still learns which two fingerprints
talked, for how long, and roughly how much traffic, same as the hybrid
double-hop's control-plane visibility would show. Raw-proxy also doesn't
plug into `relay_selection.py`'s existing reliability-ranking/consent
model at all — it's a structurally different kind of "relay" than the
async store-and-forward one, sharing a name but no code or selection
mechanism. If a fully decentralized *marketplace* of live relays (not
one well-known anchor) is ever wanted, that discovery/ranking layer
would need to be built fresh for this model rather than reusing the
async one — an accepted, deferred cost, not an oversight.

**Decision 2 (locked in) — bounded resource limits for live relay.** Modeled
on, but distinct from, the two existing bound families this touches: each
leg of a bridge is an ordinary `LinkRealtimeSession` (`netbbs.link.
transport`), already governed by its own `REALTIME_DEFAULT_*` bounds
(64-frame outbound queue, 100 frames per 10s window, 45s heartbeat lease,
5 protocol strikes); the async relay mailbox (`relay_mailbox.py`) already
bounds *its* resource at 50 envelopes per recipient. Neither transfers
as-is, because raw-proxy's actual cost shape is different from both:

- **Concurrent bridged-pair limit.** A relay running one bridge is a real
  third participant holding *two* live Noise sessions (one to each chat
  party) for that pair's entire conversation — meaningfully more
  standing cost per pair than either a single direct session or an
  at-rest mailbox entry. A new node-level `max_concurrent_relayed_pairs`
  limit (SysOp-configurable, same `nodeconfig.py` dataclass-field-plus-
  validation shape `ShutdownConfig` already uses) caps this directly,
  independent of the per-session frame-rate bound above, which caps a
  *single* session's chattiness, not how many a relay carries at once.
- **Per-pair byte-rate bound, not a frame-rate one.** `max_frames_per_
  window` counts discrete parsed frames — meaningless for raw-proxied
  bytes, which the relay by design never frames or parses at all. A
  bridge needs a bytes/second ceiling instead; exceeding it closes the
  bridge, the same "drop rather than silently degrade" precedent
  `LinkRealtimeSession.send()`'s existing slow-consumer handling already
  sets for a full outbound queue.
- **Idle-bridge timeout is protocol-agnostic, unlike the existing
  heartbeat lease.** `LinkRealtimeSession`'s own dead-peer detection
  inspects real ping/pong frames — raw-proxy structurally cannot do
  that, since the relay never sees frame semantics, only opaque
  ciphertext bytes. A bridge instead needs a dumb "zero bytes observed
  in either direction for N seconds" timer: the two actual endpoints'
  own (relay-invisible, encrypted) heartbeat traffic keeps a genuinely
  live bridge from ever tripping it, with no frame-aware logic required
  on the relay's side at all.
- **Bounded, timed-out pending-rendezvous table.** A node that shows up
  first, before its counterpart, waits — bounded in count (a cap on
  simultaneous pending requests per relay, the same "bound remotely
  influenced resources" principle `MAX_MAILBOX_ENVELOPES_PER_RECIPIENT`
  already applies to the async mailbox) and in time (a lone request
  that waits past its own timeout expires and is reported back to the
  requester as an explicit failure — CLAUDE.md's "fail clearly," not a
  request silently forgotten).
- **The existing slow-consumer-drops-the-session behavior composes
  across two hops for free, unneeding any new logic of its own**: each
  leg is its own ordinary `LinkRealtimeSession` with its own existing
  bound: a slow leg drops only that one session exactly as today, and
  the relay tears down the other leg in response (a bridge with only
  one live end isn't a bridge) — no bespoke two-hop-aware queue ever
  needs writing.

**Decision 3 (locked in) — v1 fallback UX for two mutually-unreachable
nodes.** Extends this project's own already-shipped local convention
rather than inventing a new one: `netbbs.net.chat_flow`'s `/msg`/
`/private` already require the recipient currently online, refusing
plainly ("X is not currently online.") when they aren't, with local mail
as the standing async alternative. The cross-node case gets the identical
shape: attempting live Link direct chat with a peer whose node can't
currently be bridged (no relay reachable, the relay's own pending-
rendezvous request times out, or the concurrent-pair cap above is full)
produces an explicit, never-silent refusal naming the situation plainly
(e.g. "<user> can't be reached for live chat right now.") and points at
Link mail (`link_message`/`relay_mailbox.py`, the *already-shipped* async
cross-node messaging path — not a new mechanism) as the immediate
alternative. The caller-facing message deliberately does not distinguish
*which* of the possible reasons applied — offline peer, no relay, relay
at capacity, rendezvous timeout — mirroring this project's existing
stance elsewhere (design doc §12) that such operational detail about a
*remote* node is not something a caller needs and could leak more than
intended about the other side's situation.

**Decision 4 (locked in) — rendezvous frame shape.** New frame types
added to the existing `REALTIME_FRAME_TYPES` set (`netbbs.link.
protocol`), not a new protocol version or frame family: extending that
frozenset without bumping `REALTIME_PROTOCOL_VERSION` is already this
file's own established pattern (e.g. the presence frames joined the
original subscribe/channel_message set the same way), and an old peer
encountering an unrecognized new type already fails cleanly via the
existing "unsupported real-time frame type" rejection — no separate
negotiation needed. Rides over the *requesting* node's own already-
authenticated `LinkRealtimeSession` to the relay (the ordinary session
that already exists from `relaying_for`/relay-consent setup) — no new
authentication mechanism, matching raw-proxy's own core premise of
confining new code to connection setup, never the confidentiality-
critical path:

- `relay_request` `{target_fingerprint}` — sent by either party wanting
  to reach the other through this relay.
- `relay_waiting` — the relay's reply when only this side has shown up
  so far (bounded by the pending-rendezvous timeout, Decision 2).
- `relay_ready` — sent to *both* sides once the counterpart has also
  shown up; raw-proxy byte-pumping between the two begins immediately
  after.
- `relay_reject` `{reason}` — the relay declines outright (pending-table
  full, no `relaying_for` relationship covering this pair, concurrent-
  pair cap reached) — explicit, matching Decision 3's fail-clearly
  requirement, never a silent drop.

This closes every acceptance criterion issue #168 named.

**Implemented** (§8.10.3 is the normative description): `netbbs.link.
realtime_relay` (relay server half and party half), `netbbs.link.
realtime_direct` (session establishment order, direct messages, the
reliable-node anchor connectors), `netbbs.net.link_direct` (the `/msg
user@node` flow and the receiving-side deliverer), five `[link]
live_relay_*` bounds with the defaults §8.10.3 lists, and a `relay_ready`
payload that names the relay's attach address so a party never has to
remember which address it reached the relay at. Two implementation
choices beyond the four decisions: the invitation reuses the
`relay_request` shape with the invitee's own fingerprint as target
(no fifth frame type), and a bridge is only ever offered between two
nodes both currently connected to the relay -- consent is the standing
session, so the asynchronous `relaying_for` model is not consulted.

### Issue #201 — managed netbbs.org subdomain + dynamic DNS — closed

**Goal:** since the project controls the `netbbs.org` domain, offer SysOps
an easy way to publish their board under it (e.g. `myboard.netbbs.org`)
and, for boards on residential/dynamic IPs, keep that record pointed at
the node's current address without manual DNS maintenance. Two
components, not one: a one-time subdomain *registration* (name
reservation + initial DNS record), and an optional recurring *dynamic-DNS
updater* (the node periodically checks its own public address and pushes
a record update when it changes) — a board could plausibly want the
first without the second (static IP, still wants a friendly subdomain).

**Decision 1 (locked in) — offered via a prominent prompt on first-SysOp
bootstrap or first authenticated SysOp login, not a silent default and
not a toggle a SysOp has to go discover.** Two different concerns were
in tension here and both are real: a bare opt-in-only design loses most
of the feature's actual value (the whole pitch is removing first-run
friction — a setting nobody discovers might as well not exist), but a
silent default also isn't right, because this makes the node contact and
register public presence with project infrastructure before the SysOp
has decided whether they want that at all — a private/test node would
get unexpectedly enrolled. This is the same *kind* of decision as Link
participation itself, which already isn't automatic on a fresh node
(seeds must be configured before a node reaches out and joins the mesh)
— for internal consistency, "does my node touch external infrastructure
and become discoverable" should stay an explicit choice here too, just
asked at the moment it matters instead of requiring discovery later.
Code review follow-up (PR #218): a supported persistent deployment
bootstraps its first SysOp via `netbbs.admin` and then runs headlessly
under systemd/rc.d — a literal "first daemon run" prompt has no
interactive input channel at that point and would either block startup
or silently be skipped. The prompt is anchored to an existing
interactive surface instead — first-SysOp bootstrap, or that SysOp's
first authenticated login if bootstrap itself stays a non-interactive
CLI invocation — with the accept/decline answer persisted so it is
asked exactly once, not on every subsequent login.

*Default flipped by issue #219 Decision 7:* on the shared first-run screen the
prompt's bare-Enter default is now accept (both first-run choices are pre-set
to accept so accepting everything is two keystrokes); an explicit "n" still
declines, and the decision is recorded once either way.

**Decision 2 (locked in) — the managed-service credential is a separate,
auto-generated, per-registration secret, not the node's own Ed25519 key.**
Both the self-hosted path (SysOp supplies their own dynamic-DNS
provider's credentials, no design question) and the managed path stay
available, not exclusive. For the managed path specifically: reusing the
node's existing Ed25519 key (the Link protocol trust root — Noise XX
handshake identity, canonical event signing) was considered and
rejected. The convenience argument for reuse doesn't actually hold — a
minted credential, generated at registration and stored transparently by
the node, gives the identical zero-manual-handling experience reuse
would. What reuse *would* cost for no real gain: blast-radius coupling
(a compromised node key would also hijack the SysOp's public DNS name,
not just Link identity) and coupling any future node-key rotation/
recovery work to also remember to propagate to DNS. A separate
credential keeps the two systems', and their compromise/recovery
stories, fully independent.

**Decision 3 (locked in) — name governance is first-come-first-served
plus a reserved-word blocklist and a one-name-per-node cap; a registered
name only actually goes live once the node has maintained a minimum age
of successful contact with the registration service; new registrations
service-wide are rate-limited, rejected outright once that rate is
exceeded; and total active managed registrations are capped by a
separate cumulative ceiling, refused once reached — no preventive
identity vetting, and no human review queue, beyond that.** Matches how the project
treats registration/content elsewhere (SysOp owns the trust decision,
best-effort not gatekept) — requiring identity *verification* before
registering a subdomain would add real friction against the feature's
own point, for a comparatively low-stakes resource. The blocklist covers
only the obvious cases (the project's own names, trademarks, slurs) —
not a general dispute-avoidance mechanism.

Code review follow-up (PR #218): first-come-first-served plus a
blocklist bounds *which* names can be taken but not *how many* —
without a cap, one node could hold indefinitely many names, consuming
DNS-provider records/cost and squatting desirable ones. A first fix
attempt (a bare cap of one name per node/registration-credential) was
itself found insufficient on further Codex review (PR #221): a node
identity and a registration credential (Decision 2) are both free for a
remote client to mint — an attacker can generate a fresh node identity
per desired hostname and hold all of them simultaneously, each
individually satisfying a "one-per-node" cap.

**A second fix attempt kept the one-per-node cap and added a minimum-age
gate** on when a reservation actually becomes a live DNS record,
deliberately decoupled from Decision 1's first-run opt-in: accepting the
offer at first run (or first SysOp login) still costs nothing and
happens immediately, but the node's registration *intent* is recorded,
not yet published, until the node has maintained a minimum period of
successful contact with the registration service. **This was itself
found insufficient on yet another Codex review, same PR (#223):** the
gate only costs an attacker *wall-clock time*, not *effort per
identity* — a single process can run a trivial heartbeat for thousands
of fake node identities in parallel, all maturing simultaneously, at
essentially zero marginal cost per additional name. The gate delays the
attack once; it doesn't bound how many names come out the other end of
that delay, so the DNS-provider cost/squatting problem this decision
exists to prevent recurs in full once the qualifying period passes.

**The age-gate is kept anyway, as a mild friction layer, but the actual
bound is a separate, service-enforced admission control layered on
top: a rate limit on new registrations across the whole managed
service (not per node/identity — the thing an attacker cannot multiply
by minting more identities), automatic up to that rate, with anything
beyond it originally specified as queued for the project maintainer to
review** — the same "a human reviews it" shape Decision 4 already uses
for contested-name disputes, extended from content disputes to
registration *volume*. This is what actually closes the gap:
DNS-provider record cost and squatting are bounded by how fast the
*service* will create new records at all, independent of how many
identities a single attacker can mint and age in parallel.

Two further Codex findings, same PR (#225), both about resources a
review queue would itself introduce or leave unbounded, not about the
rate limit's own logic: the queue would need its own explicit-capacity
bound (itself a remotely-influenced resource a Sybil attacker submitting
past the rate limit could otherwise fill without limit); and separately,
a rate limit alone bounds *speed*, not *total count* — an attacker
patient enough to always submit exactly at (never over) the threshold,
keeping every registration's contact alive indefinitely so nothing
qualifies as abandoned, could still accumulate an unbounded number of
active records over a long enough time. The second finding needed its
own fix regardless of the queue question: a separate cumulative cap on
total *active* managed registrations service-wide, refused (not queued)
once reached, freed only as existing registrations are voluntarily
released or genuinely abandoned (Decision 5) — the rate limit alone
never stands in for a real total ceiling.

**The review queue itself was dropped entirely during implementation
planning, not carried forward as a bounded-capacity queue.** Once a
request is queued at all, the realistic resolution is the same either
way: the maintainer hears the SysOp's explanation and decides by hand.
A capacity-bounded queue adds real code and a genuine single point of
(human) failure without actually simplifying that manual conversation —
the same outcome is reached faster, with less to maintain, by simply
rejecting outright once the rate limit or the cumulative cap is
exceeded, symmetric with each other, and telling the caller plainly
that the service is at capacity with a contact channel for the rare
legitimate exception. Both are hard-reject and service-wide. A reclaim
(Decision 5) bypasses the *rate* limiter because it is not a new
registration, but it must still fit the cumulative active-registration
ceiling and the one-active-name-per-node limit: reactivation consumes a
real active slot, and release/reclaim cycling must not create more live
rows than either bound permits.

**Decision 4 (locked in) — contested-name disputes are manual and
complaint-driven, stated as such, not implied automation.** At this
project's current scale, there is no realistic alternative to a human
(the project maintainer) reviewing a reported impersonation/abuse claim
and revoking if warranted. Documented explicitly so this isn't mistaken
for a more automated process than actually exists.

**Decision 5 (locked in) — both exit paths, voluntary release and
abandoned-node reclaim, share one deliberately generous cooldown before
a name becomes assignable to a *different* registrant; this is an
accepted, bounded residual risk, not a solved one.** §8.10 states that
"the remote node label, endpoint, DNS name, and TCP address are never
identity authority" — real *Link* node identity is verified by the
Noise XX handshake against the Ed25519-derived key, independent of how a
connection was dialed, so a reassigned DNS name cannot impersonate a
node at the Link protocol level; that fact is why Decision 4's manual,
complaint-driven dispute process can stay lightweight for
*impersonation* claims specifically. It does not make reassignment safe
in general: ordinary Telnet and plain-HTTP callers are never protected
by that handshake, carry plaintext passwords, and a caller who still has
the old hostname bookmarked after reassignment can have credentials
harvested by whoever holds the name now — even an HTTPS caller can be
handed a convincing fake board once the new registrant obtains a
legitimate certificate for the name.

A Codex review (PR #221) correctly pointed out that a finite cooldown
only *delays* this exposure, it doesn't bound it to zero — a caller can
in principle hold a bookmark longer than any cooldown. That observation
is true but was weighed against the wrong bar: zero residual risk isn't
the standard any real identifier-reassignment system actually meets.
Domain registries drop-catch expired names — commonly with *no* grace
period at all — and telecom carriers recycle phone numbers after a
dormancy window (typically 90 days to a couple of years), both fully
aware that a returning party can be phished by whoever holds the
identifier now; neither treats "never reassign" as the answer. A
permanent-retirement design was tried in this entry and reverted:
correct in principle, but strictly more conservative than the
registries and carriers this project is directly comparable to, and it
trades a security property nobody else in this space provides for an
unbounded, ever-growing cost (a permanently-retired name is retired for
the life of the project, not just until interest fades). **The bounded-
cooldown design is kept, deliberately set longer than commercial
practice needs to be** (on the order of 90 days, well past a typical
registrar's ~30–45-day redemption window) **since NetBBS has no
commercial pressure to recycle a name quickly and generosity here costs
nothing.** A SysOp choosing to leave stops their own renewal and DNS-
updater contact immediately; the name itself does not become claimable
by a different registrant until the cooldown elapses. Exact cooldown
length is an implementation-time parameter (see the closing note
below), not fixed here, but it is the same parameter for both exit
paths, not two.

**Settled without being a real fork:** how this interacts with existing
Link node addressing — a full peer's descriptor already advertises a
host/port (`advertised_host`/`advertised_port`) for other nodes to dial;
a stable managed hostname is exactly what belongs there instead of a raw
dynamic IP. No new addressing concept, no conflict with fingerprint-
based node identity, which stays the actual trust root regardless. This
covers only the Link-to-Link dial path, not the caller-facing address a
human dials — see Decision 6.

**Decision 6 (locked in) — the managed hostname's caller-facing address
is standard ports on a fixed, documented convention, not a new discovery
mechanism.** Code review follow-up (PR #218): an A/AAAA record alone
cannot tell a human caller which transport or port to use, and
`advertised_host`/`advertised_port` (Decision 5's "settled without being
a real fork" above) describes only the Link HTTP listener — a
Link-disabled or outgoing-only board has no dialable address there at
all, so reusing it doesn't by itself deliver on "myboard.netbbs.org" as
a caller-facing promise. Resolved by convention rather than a new
protocol: a managed subdomain implies the node's Telnet (23) and SSH
(22) listeners sit on their standard ports, the same assumption every
plain hostname-based BBS address already carries. Web is the same
convention with one added, non-optional requirement: `netbbs.net.
nodeconfig`'s own web listener provides no TLS of its own, only through
an external TLS-terminating reverse proxy, so "web (443)" specifically
means that proxy bound to 443 and forwarding to the node's loopback web
listener — never NetBBS's own listener bound to 443 directly, which
would silently serve plaintext HTTP (including password entry) on the
port every caller assumes is HTTPS (code review follow-up, PR #221).
A board that cannot or will not run on standard ports (web's TLS-proxy
requirement included) keeps its managed DNS record (useful for the
dynamic-IP-tracking half of this feature alone) but does not get a bare
`myboard.netbbs.org` caller address as part of it; publishing a
nonstandard port remains the SysOp's own responsibility to communicate,
same as today.

**Decision 7 (locked in, implemented) — the managed-DNS credential is
in scope for node backup/restore, as an addition to §13.4's contract,
not a separate ceremony.** Code review follow-up (PR #218): §13.4
already treats a node's recoverable state as one atomic set of specific
artifacts (database, node identity, SSH host key, banners) precisely
because a partial backup silently loses things a SysOp needs after
restoring from disk loss. The managed-DNS credential (Decision 2) is
exactly that kind of durable node state — without it, a restored node
cannot update, voluntarily release, or benefit from Decision 5's
same-owner reclaim window for its existing registration. Its credential
file (`netbbs.managed_dns.credential`) joined §13.4's backup manifest as
its thirteenth artifact, the same plain-file-copy handling already used
for node identity and the SSH host key — see §13.4's own table.

**Implemented.** Node-side client (`src/netbbs/managed_dns/`, shipped
inside the installable `netbbs` package: opt-in prompt, credential
storage, the periodic heartbeat/updater task, the SysOp status/register/
release admin screen) and the managed-service backend
(`services/managed_dns/`, a separate deployable the project itself
operates, never packaged into a node install, the same relationship the
already-live netbbs.org website has to its own separate deployment) —
including a real `Rfc2136DnsProvider` (TSIG-signed RFC 2136 dynamic
updates against a self-hosted BIND server, this project's own
infrastructure choice, not a commercial DNS API) behind the same
`DnsProvider` interface a `LoggingDnsProvider` satisfies for every
automated test. Decision 3's originally-locked review queue was dropped
during implementation planning (see that decision's own closing
paragraph above) in favor of a simpler, symmetric hard-reject shape for
both the rate limit and the cumulative cap. Shipped implementation-time
parameters, all constructor-injectable and reasoned defaults rather than
values this design doc fixes: a 24-hour minimum-age qualifying period
(Decision 3), a 5-per-hour-refilling, 5-capacity service-wide
registration rate limit (Decision 3), a cumulative cap of 1000 active
registrations (Decision 3), a 7-day no-contact abandonment threshold
before a registration is swept as abandoned, and a 90-day cooldown
shared by both voluntary release and abandonment (Decision 5, "on the
order of 90 days" as locked in above). Actually standing the backend up
— a host, DNS delegation, a real BIND server's `allow-update` ACL and
matching TSIG key — is an operational step the code does not perform on
its own; see `services/managed_dns/README.md`.

The minimum-age period measures uninterrupted successful heartbeat
contact, not wall-clock time since registration: first contact starts
the window and a gap beyond the abandonment threshold resets it. DNS
provider mutations run outside the HTTP event loop and share one bounded
transition lane with their surrounding database mutation. A sweep,
heartbeat, release, or reclaim therefore cannot commit from state made
stale by another provider await; concurrent HTTP transitions receive a
retryable rejection before entering the worker queue. Publication and
deletion failures remain retryable state (release is not finalized until
deletion succeeds, and a failed static publication is retried), and the
service-wide token-bucket state survives backend restarts.

### Issue #219 — Reliable Link as default onboarding infrastructure

**Goal:** turn Reliable Link (the project's own persistent, internet-
reachable node, run on hosting with roughly a decade-plus operating
history) into the first entry of a small, resilient "reliable nodes"
list new installs can use to get online frictionlessly — as a default
Link seed, an async relay candidate, and (once #168 ships) a live-chat
relay anchor — without becoming a hard dependency or a single point of
failure or responsibility. Builds directly on #168 (real-time relay
protocol) and #201 (managed DNS) without duplicating either's own
decisions; this entry is the product/architecture layer that sits above
both.

**Terminology (locked in):** official prose always says "Reliable
Link" — the same "spell it out in anything official" convention already
established for "NetBBS Link" itself (never "the Link" outside casual
shorthand). "ReLink.NetBBS.org" stays the DNS-only shorthand — a
hostname, not the node's name in any document or UI copy.

**Decision 1 (locked in) — purely a configurable default, never
protocol-privileged.** Nothing in the Link protocol may treat any
reliable node's fingerprint as special, required, or protocol-blessed.
Technically indistinguishable from a SysOp typing in any other seed —
this is the concrete answer to "does this violate the no-central-master
design," not just an assertion of it.

**Decision 2 (locked in) — a list, not a single hardcoded node.**
Resilience by actually having more than one reliable node from day one,
not resilience-in-theory because the field happens to be configurable.
Reliable Link is the flagship/first entry. If it were ever to stop
operating, the intended failure mode is "some other already-established
reliable node is already in the list," not "someone has to
specifically step up" at that moment.

**Decision 3 (locked in) — discovery is hybrid.** A hardcoded fallback
list ships in source (always works, even offline or if the live
endpoint is briefly unreachable on a SysOp's very first run) plus a
live-fetched list from a stable project-controlled endpoint (under
`netbbs.org`, which the project already controls and presumably outlives
any one node), preferred when reachable. Keeps the roster current
without requiring every installed node to upgrade NetBBS itself to
learn about a change.

**Decision 4 (locked in) — one list, three consumers, not three
separate mechanisms.** Default Link seeds for peer discovery; async
relay candidates (falls out of the *existing* `relay_selection.py`
reliability-ranking automatically, once a reliable node is a known peer
with relay serving enabled — no new selection mechanism needed); and
the live-relay anchor for #168's raw-proxy design. This is also the
answer to one of #168's own still-open questions ("does live relay need
its own consent/capacity concept, or reuse the async model's?") — for
v1, neither: just try reachable reliable nodes from the same list.

**Decision 5 (locked in) — relaying carries zero Phase 4 trust
implications.** Using a reliable node as a relay is not that node
vouching for the relayed traffic's origin, and trusting a reliable node
as infrastructure is not a signal about anyone else who also relays
through it. A reachability decision, not a trust decision — matches how
existing async relay consent already works.

**Decision 6 (locked in) — a node cannot participate in Link with an
unset/placeholder display name.** Surfaced during this discussion, not
originally part of it, but adopted alongside it: today's default node
name is the literal placeholder `"NetBBS"`; every default-installed node
sharing that same display name would make human-to-human conversation
about "which node am I talking to" incoherent the moment Link
participation is easy enough that many nodes actually use it. Enforced
before Link participation, not before local-only operation — a
SysOp running a purely local board never has to touch this.

**Decision 7 (locked in) — first-run UX is one screen, two separate,
defaulted-to-accept choices, not one bundled yes/no.** Relay/seed
participation and the public DNS name (#201) are offered together on
one friendly first-run screen, each pre-set to accept (so accepting
both is a two-Enter-keystrokes path) but each independently declinable
with a plain-English explanation of what accepting means. Deliberately
not collapsed into one "get online easily" choice: both declines have a
real, coherent meaning a SysOp might actually want (relay without a
public name — mesh reachability without independent discoverability; or
a public name without relay — already has direct connectivity sorted),
and bundling them would silently reintroduce the exact consent problem
that made #201 land on opt-in in the first place, for whichever half of
that combination a given SysOp didn't actually want.

**Implemented (issue #266), with the previously-open implementation
choices settled as follows.** The live roster is
`https://www.netbbs.org/reliable-nodes.json` — a JSON object
`{"version": 1, "nodes": [{"name", "url"}, ...]}`, served as plain static
content from the project's own web host (source copy and runbook in
`services/reliable_nodes/`), fetched once a day by
`netbbs.link.reliable_nodes.run_scheduled_reliable_nodes_refresh` under the
same off-switch as the release check, bounded at 32 entries with per-field
length caps, and rejected as a whole on any other format version so an old
build keeps its last good copy. A successful fetch *replaces* the built-in
fallback rather than merging with it, so removing a node from the roster
actually stops it being dialed. `[link] enabled` is now tri-state: an
explicit TOML/CLI value always wins; a silent configuration defers to the
node-wide participation decision (`netbbs.link.onboarding`), resolved once
at startup, so accepting on the first-run screen turns Link on as an
outgoing-only node with no port to open. The participation decision also
gates whether the roster is dialed at all, independently of how Link came
to be enabled — a node upgraded in place never dials project
infrastructure until its SysOp says so. Decision 6 is enforced at the
startup boundary: with Link effectively enabled and the placeholder name,
`python -m netbbs` refuses with a `StartupError` naming the fix, the same
shape as the no-SysOp refusal; the first-run screen asks for the name
before recording an accept so a fresh install never reaches that refusal,
and the console's `Settings > Join NetBBS Link` screen refuses to accept
under the placeholder for the same reason. The first-run screen
(`netbbs.net.onboarding_flow`) lives at the two anchors issue #201's prompt
already used — `netbbs.admin`'s first-SysOp bootstrap and, as a fallback,
a SysOp's authenticated login — each choice checking its own state so an
upgraded node is asked only what it never answered. Still open: Reliable
Link's own operational configuration (relay-serving is already on by
default; live-relay capacity planning waits for issue #168).

### Issue #270 — multi-hop live relay and cross-node /private

**Goal:** close the two gaps §8.10.3's first vertical left open --
relay-of-relay live sessions and the private-conversation mode across
nodes -- without reopening the raw-proxy decision (§16 "Issue #168").

**Decision 1 (locked in) — three steps, all shipped together:** anchor
advertisement (`live_relays` in the signed endpoint descriptor),
dial-the-anchor (a requester that can reach the target's advertised relay
meets it there, single hop), and the chained bridge (two relays, A–R1–R2–B)
only when the requester cannot reach any of the target's anchors. Most
cross-anchor cases never chain; chaining is the resilience path, not the
common one.

**Decision 2 (locked in) — discovery is the target's advertised anchors,
not relay-side search.** The requester names the relay to forward toward
(`via_relay`), taken from the target's own descriptor. A relay-side
fan-out ("ask every reliable node whether B is there") was rejected: it
turns one request into up to a roster's worth, adds latency, and needs no
descriptor change only at the cost of search traffic on every miss.

**Decision 3 (locked in) — hop bound of one forward.** A forwarded request
(`hops: 1`) is never forwarded again, so a chain is at most two relays and
every relay's own caps and byte/idle bounds apply per hop. Longer chains
would need routing state no relay currently keeps and are not a v1 need.

**Decision 4 (locked in) — `/private user@node-fingerprint` ships; cross-
node `/dm` invites do not.** Private mode rides the existing live
direct-message path line by line (small); a cross-node fullscreen direct
chat needs mutual invite/accept frames and a shared room spanning two
nodes -- a separate step, roughly the size of the direct-message vertical.

All frame additions (`via_relay`, `hops`, `for_fingerprint`) ride real-time
protocol v3, unreleased at the time, so no further bump was needed.
Normative description: §8.10.3.

### Deliberately deferred without active issue

- social/M-of-N node-root recovery;
- true client-side Link-mail encryption;
- schema fingerprinting beyond SQLite `user_version`;
- Community defaults as mandatory floors/ceilings.

A deferred topic becomes normative only after an explicit design decision. Do
not infer commitment from its appearance in this list. (FidoNet/BinkP
gatewaying is tracked instead under issue #166, its own active scoping
issue, not this list.)

---

## 17. Maintaining this document

Add or change text here only when it affects:

- product semantics;
- protocol or compatibility guarantees;
- authority and trust boundaries;
- persisted data meaning;
- long-lived user or SysOp behavior;
- roadmap dependency or phase scope.

Keep one current answer per topic. Replace superseded text instead of appending
correction paragraphs. Preserve only rationale which prevents a plausible but
harmful alternative from being chosen again.

Do not add:

- numbered decision rounds;
- implementation walkthroughs;
- changed-file or test lists;
- passing-test totals;
- debugging transcripts;
- transient “next up” status;
- stale issue-resolution commentary.

Use issues for unresolved work, commit/PR descriptions for change narratives,
the engineering record for durable implementation constraints, and Git history
for archaeology.

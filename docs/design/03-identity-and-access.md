# NetBBS design — 4. Accounts, authentication, identity, and addressing and 5. Authorization, moderation, and identity attestation

This is the normative design reference for this area of NetBBS. Section numbers are preserved from the design index so existing `§n.n` references remain stable.

---

## 4. Accounts, authentication, identity, and addressing

### 4.1 Account authentication

Local users may authenticate with:

- username and password, the default path;
- an optional personal Ed25519 keypair for passwordless challenge-response
  login.

The server never needs a personal user private key. Password-only users are
expected to remain the majority and must not be treated as second-class users.

A password has a lifecycle after creation (issue #611). An account changes
its own password from the Profile screen after proving the current one; an
account with no password (key-only) sets its first one without that proof,
on the strength of the login that reached the screen. A SysOp sets a new
password on any account from that account's detail screen, or from
`python -m netbbs.admin reset-password USERNAME` when locked out of the
console; neither route asks for or reveals the old password. A password may
be cleared only while the account keeps at least one public key, the same
"never leave an account with no way in" rule key removal already applies.
Every change is audit-logged with the actor and carries no other detail. A
guest session (§4.6) cannot change the guest account's password, because it
proved no credential. The current-password proof inside a session charges the
same login throttle as the login prompt, so an unattended session is not an
unthrottled place to guess.

An account's numeric id is never given to another account on the same node
(issue #1131). SQLite reuses the highest free rowid, so before this a deleted
newest account's id went to the next registrant, and a door or BBSLink game
keyed on `user_id` handed the deleted player's saves to a stranger. The node
keeps the highest id ever used and gives each new account the next one above
it. Two limits remain. Restoring a backup takes the high-water mark back with
it, so an id issued after the backup was taken can be issued again. And the
mark starts, on upgrade, at the highest id still in use or named by a deletion
in the moderation log; an id freed with no such record (before the log named
ids, or by a hand edit) can still go to one more account. Code that must
survive either pairs the id with the account's `created_at` (the guest
designation, §4.6).

### 4.2 Registration modes

A node has one registration mode:

- `open`: self-registration creates an immediately usable account;
- `approval_required`: self-registration creates a pending account which
  cannot authenticate until approved;
- `closed`: the public registration option is absent and accounts are
  SysOp-created.

A pending account that presents the right password is told it is waiting
for approval and the connection ends (issue #835). Any other failure stays the
generic "Login failed": the distinction is made only after the credential
has matched, so it tells no one anything they could not learn by logging in.
SSH shows the same notice as an authentication banner on a password login.
It stays generic for a public-key lookup, because SSH asks that before the
client has signed anything and a public key is public. A signup that created a
pending account is not charged as a failed login attempt on its connection.

Self-registration checks the desired username as soon as it is typed, before
the password prompts, and spends a login-throttle token doing so: whether a
name is taken is the same existence answer account creation used to give,
only earlier. Beyond the grammar every account shares, a caller may not
register:

- a reserved name: `sysop`, `cosysop`, `admin`, `administrator`, `root`,
  `moderator`, `mod`, `staff`, `support`, `system`, `operator`, `postmaster`,
  `guest`, `netbbs`;
- a name containing `sysop`;
- a look-alike of a level-255 account's name.

These names are compared by a skeleton: case folded, `_ - .` dropped, and
`0/o`, `1/l/i`, `3/e`, `4/a`, `5/s`, `7/t`, `8/b`, `9/g`, `2/z`, `rn/m` and
`vv/w` folded together. The rules apply to self-registration only. A SysOp
creating an account by hand may use any name the grammar allows. Only SysOp
names are protected, not every account's, because impersonating the operator
is the harm the persona test found. Blocking look-alikes of every caller would
refuse ordinary names for no gain. A signup is turned down with Decline on the
pending account, which deletes it after a yes/no. Deletion's typed-name ritual
guards content and Link history that a never-approved account cannot have.

On an approval-required node the SysOp may set one signup question, up to 200
characters. Self-registration asks it after the password. The answer is
optional, cut to 300 characters, and stored with the question as asked. It is
shown on the pending account's detail screen and deleted when the account is
approved. It was given for that one decision, and keeping it would build a
profile nobody agreed to. Declining removes it with the account. An open node
never asks it, because nobody reads the answer before the account is usable.

Registration determines whether an account may exist and log in. Link
probation and reputation determine what an active identity may do; these are
separate axes.

### 4.3 Account levels and the usable-SysOp invariant

One integer level drives ordinary level gating. `SYSOP_LEVEL = 255` is the
reserved top level; SysOp is not a parallel role flag. Levels below 255 grant
no authority by themselves. Authority short of SysOp comes from staff
permissions (§5.6) and moderator grants (§5.2), never from a level band.

Promote, demote, disable, enable, approve, and hard-delete operations must never
leave the node with zero **usable SysOps**. A usable SysOp:

- has level at least `SYSOP_LEVEL`;
- is not disabled;
- is not pending approval.

The invariant is enforced transactionally against fresh database state, not
against a stale object supplied by a caller.

A change to an account's level, its verify-identity permission or its staff
permissions (§5.6) applies to that account's live sessions without a
re-login (issue #659), whichever process made it. Each session's account watcher re-reads the account every
few seconds, and an in-node change wakes it at once. A gain is picked up the
next time the main menu is drawn, straight away if the caller is sitting on
the menu, and nothing is interrupted. A loss interrupts the caller's current
screen and returns them to the main menu, redrawn for the new level with a
line saying what changed. Every screen was entered under the old access, the
SysOp console above all, and interrupting is the only way to reach a screen
that is waiting for a key. The interruption ends whatever the caller was
doing, a running door included. An editor keeps its text as a recoverable
draft. The SysOp console also re-checks its operator at its own menu, which
is what stops a demoted operator in the standalone CLI. Moderator grants
(§5.2) need no watcher: they are read from the database at each check, so a
grant or a revocation governs the holder's next action in every session.

**Automatic promotion** (issue #992). A SysOp can set promotion rules
(`Users ▸ Promotion rules`). Each rule raises an account from one level to a
higher one once the account is old enough, has logged in often enough and,
optionally, has posted enough.

- **When.** Rules are checked at login, after the login is counted and before
  the session takes its level. The session starts at the new level and the
  caller is told once, on the first main menu. An account that qualifies
  while away is promoted at its next login, the first time the level
  matters. Nothing runs on a timer.
- **Shape.** One rule per starting level, so which rule applies is never a
  guess. A rule never demotes and never reaches 255. One rule applies per
  login, so an account climbs a ladder of rules one step per call.
- **Who is left alone.** The guest account (whether guest login is on or
  not), pending and disabled accounts, staff and SysOps, and every account
  whose level a person has set. A level set by hand takes the account out of
  the rules, so a demotion is not undone at the next login; a starting level
  other than 0 counts as set by hand. The account screen shows whether the
  rules apply and turns them back on.
- **Counting.** Logins are counted on the account itself, since session
  history keeps only a few rows per account. Posts are the account's
  approved posts on this node, each counted once however often edited.
- **Audit.** An automatic promotion is recorded in the moderation log with
  no acting account, shown as "(system)", and names the rule.

Rules are node configuration and travel in backups.

Hard deletion preserves content provenance through denormalized display labels
or nullable author/uploader references. Personal access rows and private state
which cannot meaningfully outlive the account are deleted according to explicit
foreign-key policy.

On a node that has ever run NetBBS Link, hard deletion also retires the
username (issue #594). The opaque local user identifier of §4.5 is the
username, so on the Link an account is its name: mail is addressed to it, a
carried post's author label is built from it, the trust subject is derived
from it, and a remote attestation names it. A freed name would hand all of
that to the next registrant. The name is recorded in the deleting transaction
and refused at registration, case-insensitively, whatever the Link setting
does afterwards; "ever" is a sticky marker set when the node first starts with
Link effectively on, and seeded at upgrade, on a node that ran Link before the
marker existed, from any artifact Link leaves behind. A node that has never run Link records nothing
and its names stay reusable. Nor is the name retired of a registration that
was still awaiting approval and was never sent Link mail: such an account has
provably never had a session, so there is no Link identity to inherit. A SysOp can release a retired name, as a
confirmed and audited action. Self-service registration refuses a retired
name in the words it uses for a taken one; a SysOp surface says why and where
to release it.

### 4.4 Human-facing Link addresses

The normal human-facing cross-node address is:

`user@friendly-name` or, where a friendly name is ambiguous,
`user@canonical-dns-name`

Link endpoint descriptors carry both claims inside the node-signed hello
bundle. User-facing screens show the friendly name, qualified by the canonical
DNS name where useful, and do not expose fingerprints by default. DNS names are
unique routing names, but neither a friendly name nor DNS is cryptographic
identity authority. Transport endpoints in the descriptor's `addresses` list
are connectivity data, not presentation claims; an older peer which omits
`canonical_dns_name` does not implicitly claim its endpoint host as a DNS name.

The underlying address and every protocol/persistence relationship remain
`user@node-fingerprint`. Durable linked content -- channel scrollback, mail,
carried board posts, fetched Link files -- therefore persists the fingerprint
and resolves the home node's *current* friendly identity when rendered, so a
benign rename is followed and nothing stored has to be rewritten. When that
technical author identity has an undismissed cryptographic-identity warning,
durable board and file attribution shows the warning and full fingerprint
alongside the friendly label; browsing remains available. A node with
no authenticated profile (one that has never been admitted, or is known only by
an administratively configured fingerprint) is shown by that fingerprint rather
than a shared placeholder. A full fingerprint is available as **Technical
identity** in the relevant SysOp detail view and remains accepted as an
advanced/backward-compatible input. Friendly-name resolution must be unique;
an ambiguous presentation name is refused, and the refusal spells out the
address to type for each candidate -- `user@<technical identity>`, with the
name that node goes by -- rather than repeating the already-ambiguous claim.
DNS and friendly claims share that one
namespace, so a reference matching one node's DNS claim and another node's
friendly claim is ambiguous rather than silently preferring either. Presentation
claims and abbreviated fingerprint input likewise resolve as one candidate set:
a name which equals another node's fingerprint prefix is ambiguous rather than
silently shadowing that technical address. Abbreviated fingerprint input counts
only from six characters, the length the node map shows (issue #807): below it
a one- or two-letter friendly name collided with every peer whose fingerprint
started with the same letters -- one peer in 32 for a single letter -- and
could never be used, while a deliberate six-character imitation of a prefix is
still ambiguous. Exact full fingerprints retain precedence. Fingerprints remain
hidden in the ordinary unique-name path.

What a caller reads after the `@` is what they can type back (issue #807). A
reference also matches a node's full display label (`Name · dns.example`; the
`·` is reserved, so a label never equals another node's name), and a node name
that contains `@` is shown in double quotes -- `bob@"Cats @ Night"` -- so a
reader can tell where the user name ends; one pair of enclosing quotes is
dropped from a typed reference, which is unambiguous because no friendly name,
DNS name or fingerprint may contain a double quote. A typed address splits at
its first `@`, since a user name cannot contain one and a node name can.

A chat line, and each line of a Link private conversation, names a linked
speaker's node by its friendly name alone (issue #899): the DNS name repeated
on every line cost more width than it told anyone. Where another node this BBS
knows of claims the same name -- as its friendly name or its DNS name, the one
namespace the identity warning uses -- or this BBS claims it now or did
recently, the name is qualified -- `Name · dns.example`, or `Name · abc123`, the first six characters
of the technical identity, when the node has no DNS name -- and the node map
disambiguates a shared name the same way. Both qualified forms resolve when
typed back. The speaker is styled in parts: the brackets and the `@` muted,
the user in the speaker color, the node in its own color (`NODE_COLOR`).

The user half of an address follows the local username grammar (ASCII letters,
digits, `_`, `-`, `.`, at most 32 characters), capitals included: a name is
addressed exactly as it is displayed, and the recipient node looks it up
case-insensitively, so `OldNib@Q` and `oldnib@Q` reach the same account. A
sender's name goes out as it is spelled; an account older than the username
rules whose name falls outside that grammar cannot send Link mail, since no
reply could reach it, and is told to ask for a rename. Friendly names are compared in one Unicode
normalization form (NFC), so canonically
equivalent spellings are one name, never two claims. UI delimiters, invisible
control/format characters, and the `Unnamed linked node` and `Unknown linked
node` fallback labels are reserved and cannot be claimed as friendly names. A
complete 32-character
base32 node fingerprint is reserved too: exact fingerprint lookup has precedence,
so allowing the same shape as a friendly name would make that claim unreachable
or misleading.

Peers retain authenticated observations of all three values. A friendly-name
change under the same fingerprint is an informational continuity notice. A DNS
change under the same fingerprint is a more prominent routing notice. Reuse of
a familiar friendly or DNS name by a different fingerprint is a strong
cryptographic-identity warning. A friendly name counts as familiar when it reads
as one -- compared by §4.2's skeleton as §6.3 extends it for aliases, the key they are
checked with, so "0utBound", "Out Bound" and "OutBоund" with a Cyrillic о are
all "OutBound" (issue #900) -- while a DNS name must match exactly: it is unique
by registration, and folding would equate different real hosts. The UI explains that recovery/replacement may
be legitimate but impersonation is possible, and it does not prevent the user
from continuing. Presentation names never transfer trust or reputation between
fingerprints.
An undismissed cryptographic-identity warning continues to be shown at
interaction boundaries even if a newer benign profile change is observed;
only SysOp acknowledgement dismisses it. Bounded observation pruning therefore
retains undismissed security warnings ahead of newer benign observations. The
file catalogue re-reads the selected origin's warning after the picker returns,
before asking for fetch consent. Trust-subject details show the same warning and
full technical identity; applying an override to a warned subject requires a
fresh, default-no confirmation of that fingerprint immediately before mutation.
Neither warning prevents continued interaction. The
local node likewise retains a bounded history of its previous friendly and DNS
claims so another fingerprint cannot adopt a just-renamed local identity without
raising the same warning. Reusing a retired claim moves it to the recent end of
that bounded history, so retention follows the latest use rather than the claim's
first retirement. The last friendly/DNS pair actually advertised in an
outbound hello remains part of that collision namespace until it has been moved
into history; an inbound hello racing the local rename therefore cannot claim
the just-replaced name in the gap before the next outbound descriptor is built.
Committing a changed local friendly or canonical-DNS claim also re-evaluates
every peer descriptor already on disk against the new claim, because a peer
which claimed that name first sends no further hello merely because this node
renamed; an identical already-recorded collision is not duplicated.
Startup primes the current local friendly/DNS pair before opening the Link
listener. Thereafter the shared own-hello cache is refreshed through the
background database lane before an inbound peer is persisted and once per
outbound sync pass. Each outbound hello repeats that refresh after its network
wait and immediately before persisting the authenticated peer; candidate
fallback uses the same path. A verified peer-list refresh likewise repeats it
after its network wait and immediately before persisting any updated known peer.
Every endpoint which accepts a signed hello, including
relay-mailbox pickup, performs that refresh before peer persistence; building
the signed response itself performs no synchronous
database I/O on the event loop. Authenticated live scrollback snapshots retain
their authors' home-node fingerprints in memory as well as their friendly
labels, so the same non-blocking identity warnings remain available even before
the corresponding durable event has arrived.

High-impact consent screens retain the same non-blocking model but disclose
security context before confirmation. Offering or accepting a board-origin
transfer involving a node with an undismissed cryptographic-identity warning
shows that node's full technical identity before the default-no prompt. If an
incoming offer's authenticated origin profile is unavailable, the confirmation
falls back to the signed origin fingerprint rather than an anonymous placeholder.
Likewise, a remote-file catalogue marks warned origins with their full technical
identity and repeats that caution before the fetch confirmation, before any bytes
are transferred.

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
guided SysOp action (issue #624): **Link status → Keys** on the running node,
or `python -m netbbs.admin rotate-key` on a stopped one. Either operational
key rotates on its own, and a rotation is one of two kinds, which the root
states in the revoke it signs:

- **Routine.** The old key is *retired*. What it signed while current stays
  valid, so the node's boards, posts, files and mail remain usable by peers
  that have not received them yet, including through carriers.
- **Compromise response.** The revoke carries `"compromised": true`. Peers stop
  believing anything the old key signed, and the node signs its own stored
  objects again under the new key. A key retired routinely can be declared
  compromised later by a second revoke that says so. A node that knows the
  rotating node only by introduction learns the revoke from whoever carries
  that node's content, beside the content (§8.11, issue #914). A copy a node
  already held when it learned the revoke, signed only by the compromised key,
  is held as stale from then on: not declared, not served, and replaced in
  place by the re-signed copy when a peer offers it (§8.11, issue #672).

What counts as signed "while current" is decided per object family. A
long-lived event is checked against the current key and then every key retired
without being called compromised. Anything signed fresh for one exchange (a
hello, a request, a withdrawal) is checked against the current key only. So is
anything the issuer re-issues on rotation (trust objects and attestations,
§12). §16's issue #624 entry has the rationale. Root-key custody is part of
ordinary node backup and restore rather than requiring an HSM or offline
ceremony.
### 4.6 Guest login (issue #531)

A node may designate one **existing account** as its guest identity. Typing
that account's name at the login prompt starts a session as that user without
a password prompt.

That is the whole feature, and the boundary is deliberate: guest login is an
*authentication* shortcut and never an authorization model. The guest is an
ordinary account, so levels, per-object permissions, age and name gates,
moderation, auditing and Link trust apply to it exactly as to any other
caller, and **no code branches on whether a caller is a guest** -- with the
exceptions below: mail, what a session that signed in without a credential
may change about the account it shares or keep after the call, and the
privileges the guest account may hold. A SysOp says what a guest may do the same way they say
it for anybody else: by setting the guest account's level, and by granting or
withholding per-object access.

Three consequences follow, and are intended rather than gaps:

- Writing is not blocked structurally. A guest meeting a board's write level
  may post; the level is the mechanism.
- The account keeps its password. The designation does not touch the
  credential, and turning guest access off -- one configuration change, leaving
  the account untouched -- restores ordinary sign-in for it.

  While guest access is *on*, though, typing that name on Telnet or web signs
  in as the guest: that is the feature, and there is deliberately no second
  path that offers the password prompt for it instead. The designated account
  is a node identity rather than a person's, so a SysOp who needs to act on it
  either turns guest access off for the moment or works on it from the SysOp
  console, which reaches everything about it including its keys. SSH is
  unaffected -- a key has already proven identity before the login flow runs.
- Everything after authentication still runs, and is re-checked at the moment
  of use rather than when the designation was saved. A blocked guest is
  refused. A disabled or not-yet-approved account is refused. An account
  promoted to SysOp after being designated is refused -- otherwise designating
  an ordinary account and then promoting it would be a passwordless route to
  SysOp. And a deleted guest stops being special, falling through to the
  ordinary password prompt.
- The designation records the account's **id and creation timestamp**, and both
  must match. Neither a name nor an id alone is an identity: a name resolves to
  whatever row holds it now, and an id, though never reused in normal running
  (§4.1), comes round again after a restore from an older backup. Either alone
  would hand passwordless access to a replacement account.
- **The guest account has no mail** (issue #816, §6.4). This is the one place
  the guest is treated as a guest, because a mailbox is not an area an account
  may or may not enter: it is the account's own correspondence. Every guest
  signs in as the same account, so its inbox would be read by strangers and
  anything sent from it would go out under one name many people type into.
  No level expresses that without also closing mail to every ordinary account
  at the guest's level. "The guest account" means the account guest login
  signs in without a password right now (`guest_is_eligible`); turning guest
  login off gives it its mail back.
- A guest session **may not manage the account's credentials.** The guest is an
  ordinary account in every other respect, but whether a session may touch an
  SSH key is a question about how that session authenticated, not about the
  account. The rule covers the whole key screen, not adding alone: a caller who
  proved nothing must not be able to mint a credential that outlives guest
  access being switched off, nor to strip the keys off a password-backed
  account -- removing the primary key changes the fingerprint its Link events
  are authored under. Signing in with the account's own password reaches key
  management normally.
- A guest session **may not change what other callers see of the account**
  (issue #1073). Every anonymous caller shares it, so whatever one guest
  writes there every later guest and every other caller gets: its bio and
  bio visibility, signature, display name, location, birthdate and their
  visibility switches, the verified-badge and Link-sharing switches, whether
  it takes direct messages, read receipts, whether its name is shown on
  Previous callers, who it blocks (from Profile or from Who's online), its
  MRC settings that the node-wide bridge reads per handle (private messages,
  last-seen, nick color), its chat alias (`/nick`, which is announced and kept
  in scrollback) and its MRC hub registration (`/mrc register`, `identify`,
  `roompass`, `update password`, and raw `/mrc send`, whose free text could
  carry any of them). Like the key screen, this is a check on how
  the session got in (`authenticated_without_credential`), not on the
  account: the entries stay where they are and say why when pressed, and the
  account signed in with its own password changes all of it as before.
  Birthdate matters beyond defacement: a self-entered birthdate is what local
  `min_age` gates check when the account has no age attestation (§18), so a
  guest who could set one would have opened every age gate for every later
  guest.
- A guest session's **display settings last for the call** (issue #1073). A
  guest on a plain-ASCII or 16-colour terminal needs a character set, colour
  depth, redraw style, banner speed, editor, colour toggles, sort orders and
  so on as much as anyone, so these stay open; but one guest's choice must not
  become the next guest's. For such a session every write to the per-user
  preference store and to the sort-order store stays in memory
  (`netbbs.user_preferences.session_scoped_preferences`, entered when the
  signed-in session starts) and its reads see its own choices over the
  account's stored values. The stored values are what every guest starts
  from; a SysOp sets them by signing in as the account with guest login off.
- A guest session's **drafts are its own and last for the call** (issue
  #1075). Post and file-description drafts are files named after the account,
  so on the shared account one guest's unfinished post was offered to the
  next, and two guests at once wrote the same file. A guest call keeps its
  drafts in a directory of its own (`netbbs.guest_call`, entered with the
  preference overlay), deleted when the call ends. Kept as files rather than
  in memory because that is what both editors write: autosave, `/exit` and a
  refused save's kept draft all work as for anyone else, within the call.
- A guest session **may edit or withdraw only what it wrote during the call**
  (issue #1075). Every guest's posts and uploads carry the one account as
  author, so "your own post" meant every guest's: any guest could rewrite or
  withdraw an earlier guest's words, and the edit was carried over the Link
  as the author's. The session records the posts (by root post id) and
  uploads (Zmodem or a web link) it creates; editing, withdrawing or
  describing anything else the account wrote is refused before an editor
  opens, with the reason on screen. Nothing else lets an author change their
  own content -- deleting a post or file is a moderator's, and mail is closed
  to the guest.
- A guest session **plays the bundled doors without a save that outlives the
  call** (issue #1075). Voidrunner and War Dialer key their saves on the drop
  file's `user_id`, so every guest shared one career and one crew, and the
  callsign a guest typed showed in Voidrunner's public Hall of Fame. A guest
  call plays a bundled door under a `user_id` of its own (above any rowid, so
  never an account's) with `VOIDRUNNER_SAVE_DIR` and `WAR_DIALER_DB_PATH` in
  the call's directory: Voidrunner starts from a copy of the node's Hall of
  Fame records and War Dialer from a copy of the world (SQLite's backup API),
  so the guest sees the real standings and rivals, and nothing it does
  reaches them. The door's outbound hook answers it as a rehearsal. The
  call's save stays for the rest of the call, so a guest can leave a game and
  come back. A SysOp's own doors keep receiving the guest account: what an
  external game stores is its own, a fresh identity every call would fill a
  legacy door's player list, and whether a guest reaches the door at all is
  its level.
- **The guest account holds no privilege a SysOp grants a person** (issue
  #1075): staff permissions, identity verification, or a moderator grant
  (edit, delete or approve on a board or file area; any channel permission).
  Each would belong to every anonymous caller at once. Granting one to the
  designated account is refused where it is written, with the reason; an
  account holding one cannot be designated; and `guest_is_eligible` refuses
  it at sign-in, for a grant made before either check existed. Read and post
  grants are not privileges in this sense: they open an area to the guest the
  way its level does, which is the mechanism this section describes.
- The re-checks are applied to the row that is **ultimately returned**, not
  only to the one first resolved. The login path awaits transport I/O and then
  re-reads the account to stamp `last_login_at`; a promotion, a block or a
  deletion landing in that window would otherwise have been read too early to
  matter. A deletion is a refusal like any other, not an error.

A SysOp account may not be designated. Everything else about guest access is
policy the SysOp chooses, but a passwordless SysOp login is not a choice worth
offering.

The **pre-login notice** is a short SysOp-authored line shown above the
sign-in screen -- the place a caller learns the guest account exists at all.
Unlike the welcome banner, which is authored ANSI art placed on the node's
filesystem and deliberately neither sanitized nor wrapped, the notice is typed
in the BBS and goes through the ordinary text path. It reaches Telnet and web
callers only: SSH has proven identity before there is any pre-login moment to
use.


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

A board or file area's read or write grant lets its holder past that
resource's minimum read or write level (§5.1); the age and verified-name gates
still apply. This is how a SysOp lets a helper post on a board whose write
level is 255, such as an announcements board, without making the helper a
SysOp. A grant never lets anyone past a gate on a resource it does not cover.

Anyone holding an approve grant is told so: the main menu shows a
`Moderation (n)` entry with the number of posts and uploads waiting in their
scope, and it leads to one queue across every resource the grant covers, not
a visit to each board. A SysOp can grant moderation of every local board,
file area and channel as one action, written as the three local-blanket grants
in one transaction.

Only a SysOp can grant or revoke moderator grants of any scope, blanket or
per-object, or change node configuration. A suitably authorized Link-blanket moderator may initiate a new
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

with a grace period between expiration and deletion. Local pruning never
becomes a network-wide deletion instruction.

A revision ages from the later of its own `created_at` and its post's first
revision's (issue #793). This applies to every revision: edits, withdrawals and
moderator edits.
- **Why not its own alone:** a revision carried over the Link is stamped by its
  author's clock, display metadata that may run far behind (§7.2). Aged by its
  own stamp, an edit from a node with a badly wrong clock expired on arrival,
  and the post fell back to the revision before it. For a withdrawal, that
  re-showed the withdrawn text. Now an edit can never expire before its post.
- **A genuine later edit still keeps a post alive,** as before: its own stamp
  is the later one.
- **If the first revision was also stamped by the bad clock,** the post and its
  edits expire together, which is the accepted cost.
- **Receipt time was rejected:** ageing carried content by when this node
  received it would keep old history, carried late to a newly subscribing
  node, alive for a full maximum age.
- **Where it applies:** the sweep's expire and delete steps, the read-only
  listed count and the board rankings. A file has no revisions and ages by its
  own `created_at`.

**Pinning and keeping** (issue #675). A moderator with the board's or area's
edit permission can pin a post or file, and can keep it from expiring:
- **Where:** from the post reader (`P[i]n`, `[K]eep`) or the file area screen,
  on any approved post or file, not only one awaiting approval.
- **What a pin does:** a pinned post or file is listed first on the page a
  board or area opens on, marked "pin", in at most half the page's rows. It
  also stays in the dated listing where it was posted. So a pin the block
  has no room for is still reached by paging, and the opening page leaves
  out only the dated rows its block already shows. A page reached by paging
  or by a `[N]ew scan`/`[/] Find` jump has no pinned block, so a jump opens on
  its target.
- **How the block looks:** it sits under a labelled "Pinned" rule, parted
  from the dated rows by a plain one. On a board the labelled rule replaces
  the list's top rule, so the block costs the page one row, reserved only
  when there is a block.
- **Who sees pins:** listing a board's posts, pinned ones included, checks
  that the caller may read the board. That means the effective read level
  through the Community cascade and the minimum age, the two gates on
  entering. The name requirement gates posting, not reading, so it is not
  checked here. The trust filter (§12.8) applies to pins as to every row.
- **When `[K]eep` is offered:** only where content expires (the board or area
  has a maximum age), or where something is already kept.
- **Both flags belong to the post, not to one revision:** an edit keeps them,
  and a kept post's edit does not expire out from under it. Removing a post
  clears both.
- **Local only:** a pin is this node's own presentation and is never carried
  over the Link. A carried board's moderator pins for this node's callers.

**The author is told** (issue #678). Until a moderator decides, the author
sees their held post in the board's list, in its dated place, marked `held`
and dimmed; nobody else does. It opens read only, badged "awaiting
approval". A post with an edit of theirs held is marked `held` too, and its
reader says the edit awaits approval while it shows the current text. When
a moderator approves or rejects a held post or edit, its local author is
told:
- **once, at the main menu:** a one-line notice (`moderation_notices`), for
  example `Your post "X" on general was rejected: off topic`;
- **by mail, for a rejection:** from the system (§6.4, issue #819), with the
  reason, the moderator who decided, and the rejected text, since a
  rejection deletes the post and the author would otherwise have lost what
  they wrote. It used to come from the moderator's own account, so Reply
  wrote to the moderator personally about their decision.

Rejecting asks for a reason, which is optional (Enter leaves it out). It is
kept with the rejection record (§9.3's `post_rejections`) and goes to the
author. A carried post's author is on another node and is told nothing here,
and a moderator deciding on their own post isn't told either.

**The moderator's queue** (issue #678). Each board's and file area's queue
lists what waits with its kind (`post`, `reply`, `edit` or `file`), its
author and when it was submitted. A held reply names the post it answers; a
held edit is shown against the current text it would replace, subject
included. The SysOp console's content menu has one node-wide
`[P]ending review` queue of every held post and upload, oldest first, so
nobody has to open each board and area to find what waits.

A caller granted APPROVE on a board or file area has the same queue on its
own page: `[Q]ueue (N)` appears there while anything waits, and opens the
same decision screens without the pin and exempt keys unless they also hold
EDIT. APPROVE covers the whole
decision: it lets its holder reject a held post or upload as well as publish
it. Deleting something already published still takes DELETE.

A board's and a file area's detail screen in the console has `[H]istory`:
its moderators, meaning each grant that applies there, its own and blanket
ones, with their permissions, and under them what moderators did there,
newest first, from the moderation log (bounded, like the node's audit log).
The moderators are listed there, not on the detail screen itself, which at
80x24 has no row to spare.

**Expiry is a caller-facing boundary, not only a delisting** (issue #639).
Once a post or a file is `expired`, no keystroke a caller can press reaches
it: listings, `[/] Find` and the file area's own screens are
approved-and-current only, and knowing an exact name buys nothing. This
holds for both boards and file areas, and it is the whole of what a caller
may rely on.

Two consumers inside the node still resolve an expired row, and neither is a
caller-facing surface:

- **Reply-parent resolution.** `get_post` is deliberately unfiltered so a
  reply to a thread that expired mid-conversation still finds its parent
  (§6.1's edit chains depend on the same lookup).
- **SysOp recovery.** A file's bytes survive in content-addressed storage
  until the grace period ends. The file area's admin detail screen lists its
  expired files under `E[x]pired files`, oldest first, each with its purge
  date and `[D]ownload`: the same action the pending-file review screen
  carries, offered under the same transport rule (issue #475), so recovering
  an upload does not need shell access. The listing requires `APPROVE` on the
  area; the uploader has no view of their own, since an expired file is gone
  for them as for every other caller. Recovery is the whole of the screen:
  putting a file back in the listing is not an action, and a re-upload is how
  a SysOp who wants it listed again says so. Posts have no equivalent screen:
  a post's content is its text, and the recovery case that justifies the file
  screen does not arise.

A browser transfer link (§6.2) follows the same boundary. A download link
minted while a file was listed is refused once the file expires, its own
uploader included, and serves an expired file only to a holder of `APPROVE`
on the area, which is who the recovery screen mints it for.

A domain function returning an expired row is therefore a statement about
the domain, not a promise to callers, and the contracts of `list_files_page`,
`get_file`, `list_posts_page` and `get_post` say so in those terms. There is
no by-name file lookup: `get_file_by_name` had no caller once
`/download <filename>` was gone, and the recovery screen hands over the row
the SysOp picked rather than a name, which is not unique within an area.

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
age attestation exists, access fails closed. A birthdate is neither in the
future nor before 1900-01-01.

The caller sets these. A SysOp, or a staff member with manage accounts within
its reach (§5.6), can correct or clear the display name and birthdate from the
account screen; each edit is in the account's admin history, the birthdate's
without the date. Such an edit never touches an attestation (issue #1110).

A minimum age can also say how the age must be known (issue #1082). The age
requirement is:

- `none`, the default: a verified age attestation decides when the account
  has one, and otherwise the birthdate the caller entered;
- `verified`: only a verified age attestation counts.

It is stored and inherited exactly like the name requirement: a nullable
`age_requirement` on boards, file areas and channels, where `NULL` inherits the
Community's `default_age_requirement`, and a node-wide setting for MRC open
rooms. It means nothing without a minimum age. A verified attestation always
decides when there is one, so a verified 15-year-old is not lifted past 18 by
an older birthdate typed into the profile.

Against a `verified` requirement a caller is in one of three states. With a
verified age old enough, they pass. Old enough only by the birthdate they
entered, they are **unverified**: the resource is still listed for them, marked
"needs verification" as an unmet name requirement is, and entering it refuses
with what to do ("Ask the SysOp to verify yours"; the Staff list names who).
Too young, or with no usable birthdate, the gate hides the resource as any age
gate does. A remote author is always held to a verified age, since a remote
node's self-entered birthdate never reaches this one.

**A local SysOp passes every name and age gate.** An account at level 255 on
this node passes a minimum age, a verified-age requirement and a name
requirement without a birthdate or an attestation, the way it already overrides
every level gate and moderator grant. It is never marked "needs verification".
Staff permissions and level 254 do not count. The one rule is
`attestation.bypasses_identity_gates`, applied inside `age_gate` and
`meets_name_requirement`, so every list, entry, posting, search, transfer link,
MRC open room, access map and level-change preview follows it. A remote author
is judged by `link.remote_attestation` on their own attestations and never
reaches it. (Maintainer decision 2026-10-06; it replaces #1082's original "no
bypass".)

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

**A local SysOp verifies identity without the flag; staff need it granted.**
An account at level 255 records age and name attestations and gets the main
menu's **Verify** whether or not `can_verify_identity` is set, as it passes the
gates above. Staff permissions, the Co-SysOp preset included, never imply it:
vouching for a caller's age or name opens adult areas and can travel to nodes
that trust this one, so a SysOp grants it to a helper on its own. The staff
screen the Co-SysOp preset is applied from carries **[V]erify identity**, the
same audited toggle as **[i]** on the account, and the preset's question and
outcome name that key (issue #1115). Not a second question after the preset:
that would be a dialog chain (§3.5). One rule, `main_menu.offers_verify`, decides the
menu entry, and `attestation._require_verifier` the same when an attestation is
written. The refusals for a verified age or name say where a caller sets a
birthdate (Your profile › Name & details) and who to ask (the Staff list), and
the gate editors' help says who verifies. (Maintainer decision 2026-10-06,
issue #1103.)

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
- the issuing node's SysOp having named the receiving node as a recipient;
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
content ID. Expiry and revocation both retire an attestation, and a retired
attestation has the columns that carry its value blanked (the stored value,
the envelope and the signature) while its row stays, on the issuer and on
every receiver running this software (issue #596).

Issuance is reconciled once per sync pass against current local consent, not
performed by the screen that records it: the signature must be made by the
node's current operational key, and a caller session has no business holding
one. Each pass signs an object for every Link-visible attestation without a
live one, re-issues one approaching expiry, and signs a revocation whenever the
consent behind a live object has gone — the toggle switched off, the
attestation removed, the value re-verified, or the account deleted. A deleted
account's signed objects therefore outlive the account itself long enough to be
revoked, rather than leaving subscribers holding a live assertion about a user
who no longer exists.

An object is also reissued when the node rotates its operational signing key,
without waiting for the renewal window: a subscriber resolves only the issuer's
current key, so anything the previous one signed stops verifying at the moment
of rotation. A revocation whose target has not expired is signed again under
the new key for the same reason (issue #623); a subscriber that already holds
the first treats the second as a repeat.

Attestation ingress is bounded by total retained volume as well as by rate.
The page and per-pass limits bound how fast a configured authority can deliver
objects; a per-issuer cap on active attestations bounds how many it can
accumulate, so an authority that turns hostile cannot grow a receiver's
database without limit.

Issued lifetime is 90 days, renewed once 30 days remain. The 365-day ceiling is
what a receiver must tolerate; a shorter issued lifetime is what an issuer
chooses, because a node that goes dark cannot withdraw consent it has already
published, and the issued lifetime is the window in which an opt-out that never
reached a subscriber still leaves a live assertion standing. Overlapping the
renewal with the object it replaces is deliberate: a receiver selects the
newest unexpired record, so the handover needs no revocation, and revoking
would tell a subscriber to stop trusting a value being re-asserted in the same
breath.

Propagation is a sealed, signed snapshot per recipient (issue #632, §16): the
issuer pushes each recipient node everything it should currently hold, sealed
to that node's key, directly or through the recipient's relays (§8.5), and a
snapshot is authoritative -- what it leaves out the recipient forgets. A
snapshot holds every live attestation and every revocation whose target could
still be held; once the target has expired, absence says the same. A recipient
uses a snapshot only from an issuer it names as an attestation authority,
never from a trust reporter alone, and only objects that issuer signed about
its own users. There is no pull (issue #1046): a recipient whose descriptor
does not advertise `sealed_attestations` receives nothing until it upgrades.

What reaches whom is the issuer's decision, and separate from every decision
the receiver makes. A node sends its attestations only to the nodes its SysOp
has named as recipients, a list that starts empty, is seeded from nothing, and
is per node rather than per attribute: the caller's two toggles already decide
which attributes leave at all. A node removed from the list is sent an empty
snapshot and forgets what it held (issue #632).

A receiver forgets what it is told is withdrawn. Ingesting a revocation blanks
the stored value and envelope of the attestation it names, and each sync pass
does the same for attestations that have expired. The rows stay, because the
revocation, the effective projection and the audit trail reference them, and
because a retained content ID makes a re-offered copy of the same object a
no-op. This is the whole of what opting out can promise: the issuer stops
serving the value and removes its signed copy from its live database, a
receiver running NetBBS does the same, and nothing forces a node that copied
it to. Removal is from the live, served database and is not forensic erasure:
the write-ahead log until its next checkpoint, freed pages, and backups taken
while the value was shared can still hold the bytes.

A SysOp can see everything their node currently asserts about its own users,
which nodes any of it is given to, and stop any of it. The same screen holds
the recipient list and says so plainly when that list is empty, since
published objects beside no recipients publish nothing, and it lists each
recipient with how it was last reached, when, and whether that delivered the
current snapshot, or why the last attempt failed (issue #632). A caller sees
how many recipients hold their own value beside their own sharing toggle --
"sent to 2 nodes", "sent to 1 of 2 nodes", "not delivered yet" -- never which. The listing
names the subject, the attribute, and the expiry, but never the attested
value: it is a screen about what leaves the node, not a place a verified real
name belongs. Withdrawal clears the
subject's consent and lets the ordinary reconcile sign the revocation, so the
operator action and the sync pass can never disagree about whether an object
should exist. There is deliberately no operator way to switch sharing *on*:
propagation is conditional on the subject's own opt-in, and a SysOp who
should not be asserting something can stop asserting it or withdraw the
verification itself, neither of which requires speaking for the caller.

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

For a remote author the same gate consults the accepted remote attestation,
resolved through the Community cascade exactly as the local posting path
resolves it, at the point §12.8 puts every Link enforcement decision: before
remotely influenced persistence. A carried board post whose author fails the board's own
`min_age`/`name_requirement` is not materialized, which is the same honest
exclusion as a board this node does not carry — the signed event is retained,
nothing is projected, and the SysOp's rebuild pass materializes the post if the
attestation arrives later. The refusal is silent on the wire: telling the
network which of its users fail a local gate would disclose exactly the policy
§12.8 keeps undisclosed.

### 5.6 Staff permissions, the Staff list, and the away notice (issue #836)

A SysOp can share the node's day-to-day work without handing over the node.
**Staff permissions** are account-wide grants a SysOp gives to an account
below level 255, independent of its level:

- **Approve accounts:** approve or decline registrations waiting under
  `approval_required` (§4.2).
- **Manage accounts:** disable an account and enable it again, reset its
  password, correct or clear its display name and birthdate, edit the rest of
  its Profile, and set its level anywhere from 0 to 254. Raising an account to
  255, and deleting an account and so retiring its name (§4.3), stay with the
  SysOp.
- **Moderate everything:** act as moderator on every board, file area and
  channel on the node, local and carried, with every moderator permission of
  §5.2. Without it, a staff member moderates what their moderator grants
  cover and nothing more.

The verify-identity permission (§5.5) is shown and granted beside them, but it
remains its own grant: it is about attestation, not about running the node.

**Co-SysOp** is a preset, not a role. On an account's detail a SysOp can apply
it in one confirmed step: it sets all three staff permissions. Afterwards the
account holds exactly those permissions, and the SysOp can remove any of them
one at a time. The account detail lists the staff permissions and the
verify-identity permission among its editable fields, and a summary of the
account's moderator grants among its read-only facts, so a SysOp sees
everything an account may do in one place. The editable fields are one column
in the cursor's order and the read-only facts sit below them (issue #1119): a
two-column layout made the cursor zigzag and step over read-only rows.

**`[E]dit profile`** on the account detail opens the member's own Profile
screen, acting on their account: the same fields, sections and help they see,
from the bio, signature, location and blocked people to their display, mail
and chat settings. It is offered to whoever may manage the account (a SysOp,
or a staff member with manage accounts within reach), never on your own
account, whose Profile is on the main menu. The screen is drawn the
way the SysOp's own screens are, with the member's values, and nothing on it
reaches the SysOp's session: a character set chosen there is the member's.
Every write checks the actor's authority again (`netbbs.profile_admin`) and is
recorded in the account's admin history as `edit_profile`: a setting with its
new value, private text only as changed ("Bio changed", "Blocked someone",
"Location changed"). The display name, birthdate, password and SSH keys keep
their own staff paths and records, and the keys stay a SysOp's alone, as on
the account detail. Whether a verification is shared over Link stays the
member's own choice (§5.5). The username is not editable. A member who is
signed in sees some display changes, such as the character set, only when
they next sign in.

**Guest defaults.** On the guest account `[E]dit profile` is a SysOp's alone,
and it sets what every guest starts with. A guest's own changes last only for
their call and sit over what the guest account stores (§13, issue #1073), so
the stored values are the defaults. The screen is titled **Guest defaults** and
offers what a shared account without a credential, identity or mailbox can use:
the bio, its visibility and the signature; direct messages, blocked people
(which refuses that person's live messages to guests; the account has no
mail), the MRC settings and whether the name is shown to other callers; and the
display settings. It leaves out Name & details, read receipts, sort
preferences (nothing ever stores one for the guest account, so there is none to
clear), the SSH keys and the password. A guest can still change the display
settings, the fullscreen editor and stylized MRC names for their own call; the
rest is the shared account's, the same for every guest. Each change is recorded
as on any account.

**What staff can never do.** A staff member acts only on accounts below level
255 that hold no staff permission; a moderator-only account is within reach.
They cannot set any level to 255, grant or revoke staff permissions or
moderator grants, or reach Settings, Link, node controls, managed DNS or
backups. Every action they take is audited under their own name. So the
original SysOp cannot be demoted, disabled or locked out by a helper, and the
usable-SysOp invariant (§4.3) is never at stake in a staff action.

**The Staff console.** A staff member reaches the same `[S]` entry on the main
menu, labelled for them `[S]taff`. It opens a reduced console: a landing view
of its own, which holds the away notice below, and only the screens their
permissions reach: the accounts waiting for approval and the
account list for the account permissions, and the node-wide moderation queue
filtered to what they moderate. The screens are the SysOp console's own, not
copies, so they cannot drift apart. The SysOp's console is unchanged.

**Who is told.** The notice that accounts are waiting for approval goes to
usable SysOps and to holders of the approve-accounts permission, and to no one
else.

**The Staff list.** Every member can open a list of who runs the node: the
usable SysOps, the staff members, and the moderators with what they look
after, in the words of the account detail's grant summary. Each row shows the
date of the person's last session, not the time. A person on the list is
someone members are meant to find, so the Previous callers privacy choice does
not hide them here; the confirmation that makes someone staff or a moderator
says so. Guests and pending accounts do not see the list.

**The away notice.** A SysOp or staff member can mark themselves away, from
their console's landing view, with a message of one short line of plain
text (no pipe codes) and an optional return date. It is per person, not per
node. It shows:

- on the Staff list, beside that person;
- in the message a pending account sees at login and just after it
  registers, when every account that could approve it is away: then the
  message names the approver expected back first, their return date if any,
  and their message.

Being away changes nobody's permissions. Logging in does not end it, since a
SysOp who is away may still look in. The person ends it, or, when it has a
return date, it ends by itself once that date has passed. A notice without a
date stays until it is ended, so it never claims more than it says: wherever
it is shown it reads "away since" the day it was set, and the person's own
console landing view shows it to them each time they log in, as a reminder
to end it.

Staff permissions, the Staff list and the away notice are local to the node.
None of them is carried over Link: a staff member's moderation of carried
content follows §5.2 and §9.5 exactly as a moderator's does.

### 5.7 The access map (issue #1004)

Level gates are set in many places: on each board, file area, channel and
door, as Community defaults, and in node settings. The access map is the one
list of all of them, so a SysOp can see what a level opens and what a level
change gains or loses without opening every resource. The SysOp's level
screens (#1006-#1009) show it; this section says what it contains.

**What is on it.** One gate per thing a level opens:

- each board and file area twice, for reading and for posting or uploading;
- each chat channel (entering it) and each door (playing it);
- the node-wide gates: the node map, mail, opening new MRC rooms, and the
  SysOp console at 255.

Resources excluded from this node (§9.5) are left out, since nobody reaches
them.

**Each gate says where its level comes from:** set on the resource, inherited
from its Community's default, the system default of 0, a node setting, or
fixed (the SysOp level).

**The level a gate opens at.** Usually its own level. Posting on a board or
uploading to an area also needs its read level, since a caller who cannot
open the board cannot post on it, so a write gate opens at the higher of the
two.

**Levels only.** The map answers what a level opens. It does not claim more:

- Age, verified-name, members-only and hidden are facts about each account
  or each channel's membership. The map names them as the gate's conditions,
  such as "age 18+", rather than counting them in.
- A read or write grant (§5.2) lets one account past one resource's level.
  It belongs to that account, not to a level, so the map does not list it.
- A gate that opens for nobody right now says why: a closed board, Link off
  for the node map, MRC open rooms switched off.
- A carried Linked board's write level holds only this node's callers: posts
  carried in from other nodes are not held to it (issue #993). The map says
  so on that gate.

**The Levels screen, LAST** (issue #1007). `Users ▸ [L]evels (LAST)` opens the
Level Admin SysOp Tool, which lists the levels that
matter: 0, every level a gate opens at (switched-off gates aside), and every
level an enabled, approved account holds. Each row shows how many such
accounts hold exactly that level and what it first opens. A level's own
screen lists its gates with their level and its source, in four views: new
at this level, everything open, still closed, and open with another gate
still applying. Picking a board, file area, channel or door opens the screen
the Content menu opens for it. Any level, in use or not, can be looked at.

**Level fields say what they mean** (issue #1008). Every level field in an
editor shows, next to its value, how many enabled, approved accounts are at
that level or above, recounted from the draft as the SysOp types:

- on a board or file area, a level left to inherit shows the level it
  inherits and from where ("none: 10 from Community Market");
- a write level below the read level is counted at the read level, since
  posting needs reading, and says so;
- a Community's default read and write levels also show how many boards and
  file areas inherit them;
- channels, doors and the node settings (node map, mail, MRC open rooms) show
  the count alone.

The count is by level only; age and verified-name gates depend on each
account and are not counted in.

**Level names** (issue #1009). A SysOp can name any level from 0 to 254, such
as "Member" for 10, from the Levels screen; 255 is always "SysOp". A name is a
label: gates and accounts keep their numbers, and naming, renaming or clearing
a level changes nobody's access. The console shows a named level as
`10 (Member)` wherever it shows a level: the Levels screens, the change
preview, the user screen and the editors' level fields. The user screen's
level prompt and `[G]o to level` take a level's name as well as its number.
A name is at most 12 characters, needs a letter (so it cannot read as a
number), and is unique regardless of case. Names are node configuration and
travel in backups. Naming is recorded in the moderation log.

**From the shell.** `python -m netbbs.admin last` (`levels` also works) prints the ladder;
`levels <level>` (a number or a name) prints what a level opens and what stays
closed; `levels --user <name> --to <level>` prints the change preview for one
account. Each takes `--json`. The command only reads.

**A level change is previewed for the account** (issue #1006). Changing an
account's level from its detail screen shows what the change gains, loses and
leaves blocked for that account before anything is written. Unlike the map
itself, the preview is about one account: it counts that account's read and
write grants, so a grant that keeps a board open is no loss, and it moves
whatever another gate still keeps the account out of (age, verified name, a
members-only channel it is not in, mail for the guest account) into its own
"still blocked" list instead of the gains. Something the account was kept out
of anyway is not shown as a loss. The screen's `[A]pply` makes the change and
`[B]ack` leaves the level as it was. A change that would be refused (the last
SysOp, a level outside 0-255, a staff member raising someone to 255) is
refused before the preview, and a change that opens and closes nothing is
applied at once, its outcome line saying so.

**It agrees with the checks.** The map is built from the same effective-level
functions the checks use, and the test suite holds each gate's answer to the
real check for accounts at every threshold. Every level check in the code is
either on the map or recorded as not a gate, so a new gate cannot be left off.

---

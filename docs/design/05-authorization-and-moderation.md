# NetBBS design — §5. Authorization, moderation, and identity attestation

Part of the [NetBBS design](../NetBBS-design-doc.md). Section numbers are stable: cite this area's rules by their section numbers (§5.n).

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
4. Link-blanket, granted by an origin over every linked resource it
   originates and honoured by every carrier (§16, issue #1357). An origin may
   also delegate one resource or one Link Community.

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

**Only a local SysOp verifies themselves.** A SysOp passes every identity gate
without a verification, but a real-name FTN echo (§6.8) still needs their
verified name, so the **Verify** picker lists a SysOp's own account and
`attestation.attest_name`/`attest_age` accept it. Staff with
`can_verify_identity` vouch for others only, and the domain refuses their own
account as well as the picker leaving it out. The history entry of a
self-verification says `(self)`. A self-verification is never shared over
Link: the signed object names only the issuing node, so a remote node could
not tell it from a third party's, and the consent toggle refuses it.
(Maintainer decision 2026-10-23, issue #1443.)

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
not cause the real name to leak into unrelated screens. A board carrying an FTN
echo extends that scope to the echo: with `verified_and_displayed` its posts go
out under their authors' verified real names, and nothing else the author
writes does (§6.8, issue #1443).

A record shows what it was written under. A board post, an uploaded file or a
chat line written in a resource that displays verified names (its effective
requirement, the Community cascade included) stores the author's primary name
-- display name, else username -- and their attested real name, if any, at
that moment. A later verification, replacement or revocation, a rename and a
display-name change never alter it, so a post written before its author was
verified never gains the unit, and one written after keeps it. Every revision
of a post (an edit, a withdrawal, a moderator's edit, a tombstone) carries its
root's values. Whether the unit is shown is still decided when the record is
read: a resource that stops displaying names shows the plain label, and a
record written before the resource displayed them shows its plain label. A chat
line freezes only the attested name; its alias stays current presentation. The
stored copy is written by this node from an attestation and never received or
typed, and the unit is made from it by the same trusted renderer (issue #1453).

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
value in unrelated screens. A carried post keeps the name of the remote
attestation accepted when this node stored it, as a local post keeps its
author's (issue #1453).

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

**Pages** (issue #1377). A caller pages the node's staff from chat with
`/sysop <reason>` (§6.3). A page goes to the usable SysOps and to everyone
holding any staff permission, resolved when it is delivered and shown, and
not to moderators. It is stored in a shared Pages queue: the console
dashboard's ATTENTION panel and the Staff console's landing view count open
pages, and the main menu tells each recipient "1 page waiting: SysOp →
Pages." Online recipients get a live notice with the chat bell, routed as mail
arrivals are: at once in chat and the Monitor, queued on other screens, never
into a door or an editor. A page offers `[C]hat` (break-in, while the paging
session is connected), `[M]ail reply` (members only) and `[D]one`, and one
person handling it clears it for all. Away staff are still paged, and the
caller's answer names the one expected back first when everyone is away. The
Monitor and the node log record where the caller was, never the reason. A
caller has at most one open page, a new one replacing its reason, and pages
at most once every 10 minutes, counted per account (per session for guests).
A guest may page only while a recipient is online. Staff cannot page their
own node, and no bot can page. Open pages are kept until handled, handled ones
for 30 days. See §16, "Issue #1377 — paging the SysOp".

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
- the node-wide gates: the node map, mail, opening new MRC rooms, adding to
  the oneliner wall, and the SysOp console at 255.

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
- channels, doors and the node settings (node map, mail, MRC open rooms,
  oneliner wall) show the count alone.

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

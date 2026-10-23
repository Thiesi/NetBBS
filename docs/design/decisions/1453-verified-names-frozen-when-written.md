# Issue #1453 — verified names are frozen when a record is written — decided and built

A decision recorded in [§16](../16-decisions.md) of the [NetBBS design](../../NetBBS-design-doc.md).

On a resource whose name requirement is `verified_and_displayed`, the author
of a stored post, upload or chat line was resolved when it was read: the
account's current display name and current name attestation, or for a
Link-carried post the remote attestation accepted now. The maintainer reported
the result: a member posted with no verified name, the SysOp verified them
later, and the old post then read as `(=Real Name=)`, as if it had been posted
that way. A post's `author_label` is denormalized so history reads correctly,
and the live lookup undid that. Since issue #1443 a real-name echo also sends
the verified name at posting, so the board and the network copy could
disagree. The rule is in §5.5 (resource-scoped disclosure) and §6.8 (From
name). Maintainer decision of 2026-10-23.

**Decision 1 — a record keeps what it was shown under.** A board post, an
uploaded file or a chat line written in a resource that displays verified
names (its own requirement, else its Community's, the cascade the gate uses)
stores the primary name -- the display name, or the username without one --
and the attested real name, if the author had one. A later verification,
replacement or revocation, a rename and a display-name change leave it as it
was. Rejected: **the live lookup**, the reported bug -- a badge on an old post
says "this person was verified when they wrote this", which a later
verification makes false; and **freezing only the badge**, which would still
let a display-name change rewrite a byline the board showed under the old
name.

**Decision 2 — whether to show the unit is still asked at read time.** The
value comes from the record; whether a `(=...=)` unit appears is the
resource's current requirement. A resource that stops displaying names shows
no unit and the plain label, as before, and a record written before the
resource displayed names shows its plain label, never a name it was not
written under. The unit is still made only by `attestation.verified_name_unit`
from a value this node took from an attestation -- the stored copy is written
by this node alone, never received from a peer or typed by anyone -- so the
anti-forgery guarantee is unchanged.

**Decision 3 — an edit keeps its post's values.** Every revision row (an edit,
a withdrawal, a moderator's edit, a tombstone, locally or from Link) carries
its root's frozen values. The maintainer's first thought was to freeze at edit
time, since an edit is written now; rejected because the byline is already
read from the root row whatever the revision (`_resolve_current_version`), a
moderator's edit is not the author writing anything, and FTN never re-sends an
edit (§6.8), so an edit-time name could differ from what the network was given.
An edit is a revision of the same authored post, shown as by the same author.

**Decision 4 — a carried post keeps the remote name accepted when it was
stored.** A Link-carried post on such a board stores the name of the remote
attestation this node accepts at the moment it materializes the post, through
the same subject the board's identity gate asks. Its primary name stays its
`user@<home node>` label, presented by the home node's current friendly
identity (§4.4): that presentation is node identity, not the author's name. A
rebuild of carried posts stores them again, and so freezes from what is
accepted then.

**Decision 5 — FTN sends the frozen value.** A real-name echo's From name, and
a reply's To name, is the name frozen on the local post, so the board and the
network copy always agree, including for a post a moderator approves after the
author's attestation changed. A carried post's frozen name is never this
node's to send. Issue #1443's refusal at posting is unchanged; a post with no
frozen name still stays local with a warning.

**Decision 6 — chat freezes the unit, not the alias.** A chat line keeps the
attested name its author had when it was written. The rest of its speaker
label stays live, as chat has always shown it (§6.3: the current alias,
issue #1306's rename-following resolution): an alias is presentation, and the
chat label was never a denormalized history field. Live presence -- who is
online, `/whois` -- is not a stored record and is unchanged.

**Upgrade.** Records on resources that display names when the migration runs
are backfilled. The verified name is kept only where the `created_at` of the
author's current attestation is no later than the record's; anything else
stores none, so the
reported bug is repaired rather than frozen in. A carried post uses the
accepted remote attestation's projection and its issue time. The primary name
is the author's current display name or username, not the `author_label`
stored at posting: that is what such a post showed until now, so only the
wrongly applied badges change, and a display-name history does not exist to
recover. A post whose account is gone keeps its plain label, as before.

**Not changed.** Mail never shows resource-scoped names. Search results and
moderation screens show the plain `author_label`. Fetched Link files never
showed a remote verified name. A revocation does not remove a real name
already frozen on old records -- the same holds for the copies already sent to
an FTN network -- which is what "revocations don't alter old posts" means. An
edit keeps it too (Decision 3); a SysOp who must take one down removes the
post.

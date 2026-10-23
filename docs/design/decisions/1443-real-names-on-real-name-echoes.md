# Issue #1443 — real names on real-name echoes — decided and built

A decision recorded in [§16](../16-decisions.md) of the [NetBBS design](../../NetBBS-design-doc.md).

Some FTN echoes take posts under real names only. nixNet's policy allows
aliases "in all echoes except for NIX_ADMIN", and ReLink, which joined nixNet
on 2026-10-22 as the FTN gateway's first real network (#1135), sent its
NIX_ADMIN test message as `Carrier`, its SysOp's username. NetBBS always sent
echomail under the username, usernames cannot hold a space, and a second
account or a signature in the text leaves the sender an alias. The rule is in
§6.8 (From name, inbound netmail) and §5.5 (resource-scoped disclosure).
Maintainer decisions of 2026-10-23.

**Decision 1 — the board's name requirement is the trigger.** A board that
carries an echo and whose effective name requirement (its own, else its
Community's) is `verified_and_displayed` sends its author's verified real name
as the From name. Every other echo, local boards, Link and chat keep the
username. It extends §5.5's "disclosure is resource-scoped" to the network the
board feeds: the board already shows the real name to its readers, and the echo
is part of that board. Rejected: a separate per-board or per-echo "send real
names" setting that requires `verified_and_displayed`. It would be a second
switch with one valid combination, on a second screen, for the same idea.

**Decision 2 — netmail to a verified real name reaches its member.** A reply
on the network addresses the name the author was seen under. Inbound netmail
is matched against usernames and names renamed away from first, as before,
then against verified real names, exactly and without case or surrounding
spaces. One account with that name gets it, still subject to whether it takes
mail. No match, or two accounts with the same verified name, go to the SysOp
with the usual note: a netmail that could be for either is given to neither.
An unverified display name is never matched. Rejected: routing real-name
netmail to the SysOp only, which is fine for a SysOp but leaves an ordinary
member's correspondence in someone else's mailbox.

**Decision 3 — a post that cannot go out under the real name is refused before
it is written.** Two cases: the author has no verified real name, or it is
longer than FTS-0001's From field (35 bytes, counted in the character set the
message is written in, which `encode_message` chooses). The board page says so
in place of `[P]ost`, the cross-post picker marks the board, and writing the
post refuses with the reason, so a reply, a cross-post copy, a bot's post and
a door's post (whose label is nobody's name) all meet the same check. A local
SysOp skips every name gate (`bypasses_identity_gates`), so the requirement
alone would let them post without a verified name; this check applies to them
too. As a backstop, the exporter keeps a post that still reaches it without a
usable name local, with a node-log warning, as it does for a full queue: one
whose author's verification was revoked or replaced while it waited for a
moderator, or one carried from elsewhere. Nothing goes out under the username
or cut short. Rejected:
- **falling back to the username**, which breaks the echo's rule in the
  author's name;
- **cutting the name to 35 bytes**, as To and other From names are, which
  sends a name that is not the verified one;
- **refusing an over-long name when it is verified**, which would make
  verification depend on which boards the account might post on later.

**Replies.** A reply's To name on such a board is the parent author's verified
real name when the parent is a local post, so it matches the From the network
saw; an FTN parent keeps its own name, as before. Inbound echomail is
unaffected.

**Amendment — a SysOp verifies themselves (maintainer decision 2026-10-23).**
As built first, the **Verify** picker left out the verifier's own account, so
a SysOp could take part in a real-name echo only after a second account with
"Can verify identity" verified them. A local SysOp now finds their own account
in the picker, and `attest_name`/`attest_age` accept it; the history entry
says `(self)`. Staff with "Can verify identity" still verify others only, and
the domain refuses their own account, not just the picker. Rejected: letting
any verifier verify themselves, which would let a helper grant themselves
whatever a verified age or name opens.

A self-verification is never shared over Link. The signed
`remote_identity_attestation` names only the issuing node, so a receiving node
could not tell a SysOp's word about themselves from a third party's check.
Turning sharing on refuses it, the Link export skips it, and the Profile shows
sharing as `off (you verified it yourself)`. Rejected: marking the object as
self-attested, which changes a signed protocol object for a case a
receiving SysOp has no policy for yet.

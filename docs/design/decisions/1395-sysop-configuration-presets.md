# Issue #1395 — SysOp configuration presets — decided

A decision recorded in [§16](../16-decisions.md) of the [NetBBS design](../../NetBBS-design-doc.md).

NetBBS has many settings, each bounded and documented, and a new SysOp has to
understand most of them to know whether the defaults suit them. Issue #1395
proposed named presets that set many settings at once. Its examples were
node-wide: "small private node", "public node" and "Link hub". The maintainer
approved it for scoping on 2026-10-17. The scoping was to start with a survey of
every SysOp-facing settings area, recording for each one whether a preset would
help and what it would set.

The survey ran against v7.38.0 (2de54b9e). It is posted on the
[issue](https://github.com/Thiesi/NetBBS/issues/1395). The rule that results is
in §3.4 (SysOp operations console). Maintainer decisions of 2026-10-23.

## What the survey found

- **The node-wide examples mostly hit settings a preset cannot or must not
  set.**
  - Link outgoing-only is already the default (`[link] outgoing_only = true`).
    It is TOML-only, along with the listeners and advertised addresses, so that
    a wrong value set inside NetBBS cannot lock the SysOp out (§13.10a). A "Link
    hub" preset therefore cannot make a node a full peer.
  - A value in the config file or on the command line beats the console for
    every Operating-limits key.
  - Managed-DNS registration needs a name the SysOp chooses and a live call to
    the service.
  - There is no probation feature. Every account starts at level 0, and the
    promotion rules are the only probation there is.
  - "Baud with greetings" is already one step at onboarding.
  - Nothing holds a default for *new* things that a preset could set: the
    channel bot policy, new-account preferences and new-resource drafts all
    have none.
  - Onboarding deliberately does not bundle Link, managed DNS and Baud into one
    yes. Each one is the SysOp's consent to something (`onboarding_flow.py`'s
    docstring).
- **Area presets are already how this console works.** Five exist:
  - the Co-SysOp staff preset (§5.6)
  - the moderator-grant presets
  - the door profile presets
  - the banner art gallery
  - the fsxNet network template

  Each sits on the screen it affects and leaves every value editable
  afterwards.
- **Three areas have many interdependent values that a SysOp cannot judge
  without trying them.** Those are where a preset actually saves work:
  - the model conversation's budgets
  - Link capacity
  - the level ladder

## Decisions

Implementation is issue #1504.

**Decision 1 — presets are per area, not node-wide (locked in).** Three
presets are built:

1. **Model conversation budgets:** Frugal / Normal / Generous.
2. **Link capacity:** Small / Default / Hub.
3. **Levels ladder.**

There is no node-wide "kind of node" preset.

Once the rules below are applied, a node-wide preset keeps no setting of its own
and could only run the three area presets one after another. That chain would
still break onboarding's consent design whenever it touched Link, managed DNS,
MRC or the model conversation.

Rejected alternatives:
- **The issue's three node-wide presets**, for the reasons in the survey.
- **A node-wide menu that chains the area presets.** It adds nothing the area
  screens don't already offer.

**Decision 2 — a preset is a template, not a profile (locked in).** Applying a
preset writes values and nothing more. Apart from the audit entry (Decision 4),
the node keeps no state about which preset was applied. No screen says "you are
on Hub" or "you have drifted from your preset". A value changed later is just a
value.

Rejected: **a remembered profile**. It creates a "drifted" state that every
later edit must explain, and nothing the node does depends on knowing it.

**Decision 3 — the same shape everywhere: choose, preview, apply (locked in).**
Each preset is one key on the screen that owns its settings. The levels ladder
lives under Users ▸ Levels. The model budgets sit in the Open chat bots menu
beside Model settings, because the model editor is itself a draft.

Applying a preset goes through four steps:
1. **Choose.** The key opens a short pick of tiers.
2. **Preview.** The chosen tier shows a detail panel (§3.4) of every value it
   would change, as `before → after`, grouped the way the owning screen groups
   them. The panel leaves out values that are already at the target, and lists
   values the preset cannot change under their own heading, each with the
   reason.
3. **Warn.** Any warning the tier carries comes next.
4. **Apply.** Nothing is written until the SysOp confirms. The outcome is
   announced in the next redraw (§3.4), and so is when it takes effect.

`python -m netbbs.admin` shares the console, so it offers the same presets.
Onboarding does not offer them. A preset tunes something the SysOp has already
switched on, and onboarding's three questions are about switching things on.

**Decision 4 — one transaction, one audit entry (locked in).** Applying a preset
writes every value and a single `record_action` entry in one transaction. The
action names the preset and tier, and the detail lists every changed
`key=before→after`.

This follows the existing Limits & retention and Operating-limits saves. Setters
that commit or audit on their own (`save_promotion_rules`, `set_level_name`) get
variants that do neither, so the preset owns the transaction.

**Decision 5 — what a preset never touches (locked in).** A preset never sets
any of the following:
- secrets: API keys, tokens and passwords
- external hosts, ports and endpoints
- addresses, paths, certificates and pins
- identity and key material
- trust configuration tied to fingerprints
- the node name and timezone
- per-user preferences
- the SysOp's own text: the registration question, the welcome letter,
  announcements and personas
- anything that sends data off the node: Link participation, managed DNS, MRC,
  enabling the model conversation, logging prompts, letting guests use the
  model

The last group is the only one a preset may change at all, and only to turn
something off, never on. Turning one on stays behind its existing confirmation.

**Decision 6 — Model conversation budget tiers (locked in).** The tiers set only
the eight bounded numbers:

| Field | Frugal | Normal | Generous |
| --- | --- | --- | --- |
| `day_tokens` | 25,000 | 100,000 | 1,000,000 |
| `month_tokens` | 250,000 | 1,000,000 | 20,000,000 |
| `user_daily` | 5 | 20 | 100 |
| `cooldown` (s) | 60 | 10 | 5 |
| `channel_per_minute` | 1 | 2 | 6 |
| `output_tokens` | 128 | 256 | 512 |
| `reply_chars` | 300 | 400 | 500 |
| `context_lines` | 2 | 6 | 10 |

- Normal is today's defaults.
- The money budgets and prices stay out: the currency and the prices are the
  SysOp's own.
- The tiers apply live.
- The preview warns when a tier raises the token budgets on a node with no money
  budget set, because nothing then caps spend except the token counts.
- The preset works whether or not the model conversation is enabled, and does
  not change whether it is.

**Decision 7 — Link capacity tiers (locked in).** The tiers live on
Settings ▸ Operating limits, beside the per-group editors. They cover the
carry caps, peering, Link limits and real-time relay groups.

| Setting | Small | Default | Hub |
| --- | --- | --- | --- |
| Carried boards / channels / file areas | 50 each | built-in | 2,000 each |
| Peers remembered | 200 | built-in | 5,000 |
| Relay for others | — | built-in | on |
| Relay clients | — | built-in | 100 |
| Files per carried area | 1,000 | built-in | 20,000 |
| Request burst / per minute | — | built-in | 60 / 180 |
| Diagnostics kept (days / entries) | 14 / 2,000 | built-in | 90 / 20,000 |
| Live bridges / pending rendezvous | — | built-in | 32 / 128 |
| Relay bytes per second | — | built-in | 262,144 |

- "—" means the tier leaves the value as it is.
- **Default removes the console's stored value** rather than writing today's
  number, so a future change to a built-in default reaches the node.
- **Not covered:**
  - the login throttle, since loosening it weakens brute-force protection;
  - the shutdown delays, which are not capacity;
  - the seeds and the sync interval, which say whom and how often the node
    dials, not how much it holds.
- **What the preview must show:**
  - Keys the config file or command line sets are listed as "set in config —
    not changed", from the startup snapshot.
  - The values apply at the next start, as every Operating-limits value does.
- **Hub preview warnings:**
  - higher carry caps let other nodes fill this node's storage;
  - relaying spends bandwidth on nodes the SysOp does not know;
  - on an outgoing-only node, relay serving does nothing. The panel shows the
    TOML a full peer needs (`outgoing_only = false`, the advertised host and
    ports) as a step the SysOp still has to take, not one the preset took.

**Decision 8 — the levels ladder (locked in).** One preset, offered under
Users ▸ Levels. It sets four things:

- level names: 0 "New", 10 "Member";
- one promotion rule, 0 → 10 after 24 hours and 2 logins (the SysOp Handbook's
  own suggestion);
- `mail_min_level` = 10;
- `oneliner_min_level` = 10.

It is one preset because its parts only make sense together: a rule up to 10
means nothing unless something is gated at 10.

- It replaces only what it names. A name the SysOp gave level 0 or 10, or an
  existing rule from level 0, shows in the preview as `before → after`. Other
  names and rules stay.
- The changes apply live.
- **Preview warning.** Accounts at level 0 can no longer send mail or write
  on the Wall until the rule
  raises them, which happens at their next login once they qualify. The rule
  never moves an account whose level was set by hand, nor staff. The preview
  counts these held-back accounts so the SysOp can raise them by hand.

Users ▸ Levels (LAST) already shows what each level opens. After the preset it
shows the result.

**Decision 9 — not built (locked in).**

- **Declined:**
  - **MRC privacy-first / open.** The "open" tier's substance would be sending
    callers' IP addresses and metadata to the hub. Issue #377 left that to the
    SysOp switch by switch, and Decision 5 excludes it. Without it, only five
    open-room fields on one screen are left. Those gates are also copied into a
    room when it opens, so a tier change would only reach rooms opened
    afterwards. Clearer help text on that screen serves the SysOp better.
  - **Registration, guests, trust, updates, the login throttle, announcements
    and door outbound.** Each has a single choice, values tied to one node or
    identity, or defaults that should not be loosened.
- **Left out, because the gain is too small:**
  - backups, presentation and timestamp formats, each three fields or fewer;
  - Limits & retention, whose values are independent.
- **Deferred, then decided:** FTN presets (file-request tiers, more network
  templates beside fsxNet). They waited until the FTN round (#1154) had
  landed, so they wouldn't land on a subsystem that was still changing.
  Decisions 10-12 settle them.

## FTN and QWKnet, decided 2026-10-24

The FTN round's stages A-E had all merged by then (main at 8fb9c078). The same
survey was then run over the FTN and QWKnet settings:
- network, netmail, bundles and file requests;
- links, hub behaviour, robot areas and publishing;
- hatching and Origin;
- QWK offline mail, and QWKnet networks and nodes.

Its findings:
- **One gap is real: there is no choice of network.** Today [C]reate always
  pre-fills a single hard-coded network: fsxNet for FTN, DOVE-Net for QWKnet.
- **Nothing else has a set of interdependent numbers for a preset to tune.**
  - Hub behaviour (fan-out, transit netmail, the AreaFix server) has no
    settings. A node becomes a hub by having links.
  - Netmail, bundles, hatching and Origin are single toggles.
  - Links, publishing and QWKnet nodes hold values that belong to one link,
    one network or one node.
- **File requests have four limits and two switches.** The two switches are
  "Answer requests" and "Secure links only". Decision 5 keeps both out of any
  preset, because the first sends files off the node and the second loosens
  who may ask.

Maintainer decisions of 2026-10-24.

**Decision 10 — network templates on [C]reate (locked in).** [C]reate a
network first asks which network to start from, and fills the new draft from
that choice. The templates are data, so more can be added later. Building it
is issue #1517.

- **FTN:** fsxNet, nixNet, FidoNet and Blank.
- **QWKnet:** DOVE-Net only. With one template there is no pick, and the
  draft opens on it as it does today.

**Decision 11 — what a template fills (locked in).** A template is the template
of Decision 2, applied to a draft that has not been saved. Nothing is written
before [S]ave, and every value stays editable.

- **It fills the network's public facts:**
  - the name and domain;
  - the robot names;
  - the nodelist echo and its file names.
- **It fills an uplink and host only where the network has one documented
  public entry hub.** fsxNet's 21:1/100 and DOVE-Net's VERT qualify. nixNet
  assigns regional hubs and FidoNet's Net coordinators assign uplinks, so
  both leave the uplink empty.
- **It never fills:**
  - passwords, the TLS pin, or cert and key paths;
  - this node's address;
  - the listener host and ports.
- **It never switches on** Enabled, Answer calls, file-request Answer, Serve
  nodes, or giving held mail to callers without a password.
- **It never turns off** "secure links only".

Filling a host here does not contradict Decision 5. Decision 5 is about
changing saved settings. Here the SysOp sees the host in the draft and confirms
it by saving.

- **Facts come from the network itself.** A value not confirmed against the
  network's infopack is left empty rather than guessed. AgoraNet, Micronet and
  Araknet are added once their facts are in hand.

**Decision 12 — no other FTN or QWKnet presets (locked in).**

- **Declined:** file-request tiers. Without the two switches, a tier would
  only move four numbers whose defaults are sound. Field help explains what
  each limit caps (#1514).
- **Declined:** a "leaf vs hub" tier. Hub behaviour has no settings to set.
- **Declined:** presets for netmail, bundles, links, publishing, hatching,
  Origin, QWK offline mail and QWKnet nodes, for the reasons in the findings
  above.

The same survey found FTN and QWKnet saves whose audit entries leave out values
they write, and some that audit in a separate transaction. They are fixed as
bug #1515, in the same way as #1505.

## Found by the survey, decided alongside

Some settings changes write no audit entry, or an incomplete one:
- model conversation saves;
- the managed-DNS opt-in;
- the Link answer given at onboarding;
- MRC saves, whose entry leaves out the open-room gates and the blocklist;
- FTN network saves, whose entry leaves out the node-wide listener keys they
  also write.

The maintainer approved fixing these as a separate bug, issue #1505. A secret is recorded
only as changed or unchanged, never as its value.

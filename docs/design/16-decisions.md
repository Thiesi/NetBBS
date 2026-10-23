# NetBBS design — §16. Decisions and extension references

Part of the [NetBBS design](../NetBBS-design-doc.md). Section numbers are stable: cite a decision as the §16 entry for its issue.

## 16. Decisions and extension references

GitHub issues are authoritative and may evolve beyond this summary.

Each decision lives in its own file under [`decisions/`](decisions/), named after its issue. Add a new decision as a new file there and a row here; edit an existing decision in its own file. The area section a decision affects remains the normative statement of the rule; the decision file keeps the reasoning and the rejected alternatives.

| Issue | Decision | State |
| --- | --- | --- |
| #11 | [canonical Link format](decisions/0011-canonical-link-format.md) | — |
| #55 | [trust and quarantine](decisions/0055-trust-and-quarantine.md) | implemented; operational validation open |
| #56 | [unread, follows, activity, and search](decisions/0056-unread-follows-activity-and-search.md) | — |
| #60 | [production operations](decisions/0060-production-operations.md) | closed |
| #63 | [door isolation](decisions/0063-door-isolation.md) | superseded by the shipped door model |
| #72 | [node-local arrival order for unread state](decisions/0072-node-local-arrival-order-for-unread-state.md) | closed |
| #74 | [FTS index integrity checks and rebuild tooling](decisions/0074-fts-index-integrity-checks-and-rebuild-tooling.md) | closed |
| #78 | [decompose LinkNode protocol state](decisions/0078-decompose-linknode-protocol-state.md) | closed |
| #80 | [end-to-end regression tests for cross-subsystem Link orchestration](decisions/0080-end-to-end-regression-tests-for-cross-subsystem.md) | closed |
| #81 | [supported platform tiers](decisions/0081-supported-platform-tiers.md) | closed |
| #82 | [operator-ready installation and release path](decisions/0082-operator-ready-installation-and-release-path.md) | closed |
| #85 | [inventory/pull-based catch-up and multi-hop relay](decisions/0085-inventory-pull-based-catch-up-and-multi-hop.md) | — |
| #86 | [event/dedup retention](decisions/0086-event-dedup-retention.md) | — |
| #87 | [linked channels](decisions/0087-linked-channels.md) | closed |
| #88 | [board closure, moderator edits, tombstones](decisions/0088-board-closure-moderator-edits-tombstones.md) | closed |
| #89 | [remote file catalogue and chunk transfer](decisions/0089-remote-file-catalogue-and-chunk-transfer.md) | closed |
| #90 | [tier-2 Link message routing scope](decisions/0090-tier-2-link-message-routing-scope.md) | deferred |
| #91 | [wire linked-channel messages into the live chat send path](decisions/0091-wire-linked-channel-messages-into-the-live-chat.md) | closed |
| #92 | [interactive browse/fetch UI for remote file catalogues](decisions/0092-interactive-browse-fetch-ui-for-remote-file.md) | closed |
| #93 | [inventory/pull catch-up for linked file-area catalogues](decisions/0093-inventory-pull-catch-up-for-linked-file-area.md) | closed |
| #165 | [MRC gateway scoping](decisions/0165-mrc-gateway-scoping.md) | closed |
| #166 | [FidoNet/BinkP gateway](decisions/0166-fidonet-binkp-gateway.md) | decided |
| #168 | [real-time relay for Link direct chat](decisions/0168-real-time-relay-for-link-direct-chat.md) | — |
| #194 | [trusted scrollback-on-join](decisions/0194-trusted-scrollback-on-join.md) | closed |
| #200 | [War Dialer: async multiplayer BBS-crew door game](decisions/0200-war-dialer-async-multiplayer-bbs-crew-door-game.md) | closed |
| #201 | [managed netbbs.org subdomain + dynamic DNS](decisions/0201-managed-netbbs-org-subdomain-dynamic-dns.md) | closed |
| #219 | [Reliable Link as default onboarding infrastructure](decisions/0219-reliable-link-as-default-onboarding.md) | — |
| #270 | [multi-hop live relay and cross-node /private](decisions/0270-multi-hop-live-relay-and-cross-node-private.md) | — |
| #474 | [foreign-platform native doors in a VM](decisions/0474-foreign-platform-native-doors-in-a-vm.md) | closed |
| #511 | [HTTP transfer hardening](decisions/0511-http-transfer-hardening.md) | closed |
| #561 | [the Link carry model: what the cap decides, and what "declined" means](decisions/0561-the-link-carry-model-what-the-cap-decides-and.md) | — |
| #589 | [automatic equivocation signals](decisions/0589-automatic-equivocation-signals.md) | decided |
| #589 | [trust-object issuance](decisions/0589-trust-object-issuance.md) | slice 1 decided and built; signals open |
| #594 | [a deleted account's username stays retired on a Link node](decisions/0594-a-deleted-accounts-username-stays-retired-on-a.md) | closed |
| #596 | [who may pull an attestation, and what opting out retracts](decisions/0596-who-may-pull-an-attestation-and-what-opting-out.md) | closed |
| #611 | [password change and reset](decisions/0611-password-change-and-reset.md) | closed |
| #624 | [guided operational-key rotation](decisions/0624-guided-operational-key-rotation.md) | closed |
| #627 | [what a node nobody can dial issues](decisions/0627-what-a-node-nobody-can-dial-issues.md) | trust objects closed, attestations open |
| #628 | [real-time Link through an HTTP `CONNECT` proxy](decisions/0628-real-time-link-through-an-http-connect-proxy.md) | — |
| #630 | [content from a node this one has never met](decisions/0630-content-from-a-node-this-one-has-never-met.md) | closed |
| #632 | [delivering attestations from a node nobody can dial](decisions/0632-delivering-attestations-from-a-node-nobody-can.md) | decided |
| #639 | [what expiry means to a caller](decisions/0639-what-expiry-means-to-a-caller.md) | closed |
| #672 | [carriers holding copies signed by a compromised key](decisions/0672-carriers-holding-copies-signed-by-a-compromised.md) | decided |
| #675 | [an author's withdrawal on the Link](decisions/0675-an-authors-withdrawal-on-the-link.md) | — |
| #711 | [an art post's layout on the Link](decisions/0711-an-art-posts-layout-on-the-link.md) | — |
| #761 | [the SysOp's live session monitor, snoop and break-in chat](decisions/0761-the-sysops-live-session-monitor-snoop-and-break.md) | decided |
| #767 | [the node map](decisions/0767-the-node-map.md) | decided |
| #836 | [delegation short of SysOp](decisions/0836-delegation-short-of-sysop.md) | decided |
| #843 | [aliases that pass for the SysOp; invitations nobody saw](decisions/0843-aliases-that-pass-for-the-sysop-invitations.md) | decided |
| #899 | [the speaker label on a chat line](decisions/0899-the-speaker-label-on-a-chat-line.md) | decided |
| #900 | [look-alike node names](decisions/0900-look-alike-node-names.md) | decided |
| #914 | [a compromise reaching a node that knows the signer only by introduction](decisions/0914-a-compromise-reaching-a-node-that-knows-the.md) | decided |
| #929 | [CP437 terminals: a character set per session](decisions/0929-cp437-terminals-a-character-set-per-session.md) | decided |
| #929 | [SysOp art: SAUCE and live slots](decisions/0929-sysop-art-sauce-and-live-slots.md) | decided |
| #992 | [automatic level promotion](decisions/0992-automatic-level-promotion.md) | decided |
| #993 | [who may post on a Linked board](decisions/0993-who-may-post-on-a-linked-board.md) | decided |
| #1004 | [the access map](decisions/1004-the-access-map.md) | decided |
| #1036 | [self-verifying evidence is verified](decisions/1036-self-verifying-evidence-is-verified.md) | decided |
| #1081 | [a console resource opens on its own fields](decisions/1081-a-console-resource-opens-on-its-own-fields.md) | decided |
| #1082 | [a minimum age can require a verified age](decisions/1082-a-minimum-age-can-require-a-verified-age.md) | decided |
| #1104 | [Linked resources by colour](decisions/1104-linked-resources-by-colour.md) | decided |
| #1105 | [a gated resource names its gates on entry](decisions/1105-a-gated-resource-names-its-gates-on-entry.md) | decided |
| #1110 | [a SysOp corrects a caller's display name and birthdate](decisions/1110-a-sysop-corrects-a-callers-display-name-and.md) | decided |
| #1111 | [renaming a username](decisions/1111-renaming-a-username.md) | decided |
| #1154 | [the FTN round before the feature freeze](decisions/1154-ftn-round-before-the-freeze.md) | decided; stages A-E tracked in #1461 |
| #1156 | [how an MRC sender's styled name is shown](decisions/1156-how-an-mrc-senders-styled-name-is-shown.md) | decided |
| #1158 | [keys that work everywhere](decisions/1158-keys-that-work-everywhere.md) | decided |
| #1165 | [public node pages on www.netbbs.org](decisions/1165-public-node-pages-on-www-netbbs-org.md) | decided |
| #1171 | [release and guest-readable boards on node pages](decisions/1171-release-and-guest-readable-boards-on-node-pages.md) | decided |
| #1208 | [chat bots](decisions/1208-chat-bots.md) | decided |
| #1214 | [Voidrunner on other BBS software](decisions/1214-voidrunner-on-other-bbs-software.md) | decided |
| #1223 | [Retro Trivia and War Dialer as standalone doors](decisions/1223-retro-trivia-and-war-dialer-as-standalone-doors.md) | decided |
| #1298 | [cross-posting a message to several boards](decisions/1298-cross-posting-a-message-to-several-boards.md) | decided |
| #1299 | [polls on message boards](decisions/1299-polls-on-message-boards.md) | decided |
| #1302 | [audible chat bells](decisions/1302-audible-chat-bells.md) | decided |
| #1308 | [Link Communities](decisions/1308-link-communities.md) | decided |
| #1354 | [the New Year and clock changes in chat](decisions/1354-the-new-year-and-clock-changes-in-chat.md) | decided |
| #1357 | [Link moderation: delegated moderators for linked boards and channels](decisions/1357-link-moderation-delegated-moderators-for-linked.md) | decided |
| #1368 | [Link DMs: cross-node `/dm` direct chats](decisions/1368-link-dms-cross-node-dm-direct-chats.md) | decided and built |
| #1372 | [private bot replies](decisions/1372-private-bot-replies.md) | decided |
| #1373 | [titled block replies for bots](decisions/1373-titled-block-replies-for-bots.md) | decided |
| #1374 | [a bot developer kit](decisions/1374-a-bot-developer-kit.md) | decided |
| #1375 | [Baud reminders: absolute times and delivery when the owner is back](decisions/1375-baud-reminders-absolute-times-and-held-delivery.md) | decided |
| #1376 | [Baud `!tell`: a note delivered on the next join](decisions/1376-baud-tell.md) | decided |
| #1377 | [paging the SysOp](decisions/1377-paging-the-sysop.md) | decided |
| #1378 | [door events as a bot announcement source](decisions/1378-door-events-as-a-bot-announcement-source.md) | decided |
| #1379 | [Baud social commands](decisions/1379-baud-social-commands.md) | decided |
| #1380 | [scheduled SysOp announcements in chat](decisions/1380-scheduled-sysop-announcements-in-chat.md) | decided |
| #1382 | [a help-desk mode for Baud's model conversation](decisions/1382-a-help-desk-mode-for-baud.md) | decided |
| #1391 | [the oneliner wall](decisions/1391-the-oneliner-wall.md) | decided |
| #1394 | [splitting the design document by area](decisions/1394-splitting-the-design-document-by-area.md) | decided and built |
| #1443 | [real names on real-name echoes](decisions/1443-real-names-on-real-name-echoes.md) | decided and built |
| #1453 | [verified names are frozen when a record is written](decisions/1453-verified-names-frozen-when-written.md) | decided and built |
| — | [Staff edit a member's Profile](decisions/staff-edit-a-members-profile.md) | decided |
| — | [SFTP over the SSH transport](decisions/sftp-over-the-ssh-transport.md) | declined |
| — | [Deliberately deferred without active issue](decisions/deliberately-deferred-without-active-issue.md) | — |

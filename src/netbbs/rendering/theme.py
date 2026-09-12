"""
NetBBS's chosen color palette — the actual color numbers used across
screens, kept in one place so screens stay visually consistent and a
future palette change doesn't mean hunting through every module that
prints something.

Deliberately restrained (a header color, an accent color, a muted color
for system/meta messages, a distinct color for valid menu inputs) rather
than a full theming system, which doesn't exist yet and isn't needed for
the current, still-small set of screens. Every screen that prints
anything colored should pull from here rather than picking its own
numbers — the gap this module fixes is exactly that boards and chat had
started drifting toward defining their own local color constants
independently.
"""

from __future__ import annotations

HEADER_COLOR = 51  # bright cyan — section headers, banners; also the chat status
                   # line's own online/away counts, distinct from ACCENT_COLOR
                   # since that already means "channel name" there and reusing it
                   # for the numbers would make two unrelated fields look like one
ACCENT_COLOR = 220  # gold — navigable items: board/channel names, other users' names
MUTED_COLOR = 244  # gray — system/meta messages (join/leave notices, etc.)
LABEL_COLOR = 75  # light blue — field names such as "From:"/"Date:";
                  # distinct from the value beside them and from HEADER_COLOR
VALUE_COLOR = 252  # soft white — ordinary field values and prose content
METADATA_COLOR = 244  # gray — timestamps, counts, and secondary context;
                      # deliberately the same shade as MUTED_COLOR but a
                      # separate semantic role for future palette changes
SUCCESS_COLOR = 82  # vivid green — completed user actions and healthy states
ERROR_COLOR = 196  # red — failed actions and unavailable/error states
MENU_KEY_COLOR = 46  # bright green — the actual valid keystroke in a menu option
SELF_COLOR = 201  # bright magenta — the user's own name/messages in chat, distinct
                  # from ACCENT_COLOR (used for everyone else's), so a user's own
                  # messages visually stand out from the rest of the conversation
CHAT_BODY_COLOR = 252  # soft white — ordinary direct-chat message text, kept
                       # distinct from SELF_COLOR/ACCENT_COLOR identity labels so
                       # a conversational line reads as speaker plus content
NICK_COLOR = 39   # sky blue — a `/nick` alias shown alone in the live chat stream
                  # (design doc), distinct from ACCENT_COLOR/SELF_COLOR so
                  # "this is a stand-in name, not necessarily the account's own" reads
                  # as its own visual category rather than blending into either
VERIFIED_COLOR = 82  # vivid green — a SysOp-verified real name (design doc),
                     # applied to the whole "(=name=)" unit at render time,
                     # directly from the trusted attested_value, never derived from
                     # user-supplied text — see netbbs.attestation's module
                     # docstring for why that's the actual anti-forgery property
CHANNEL_TYPE_COLOR = 208  # orange — a channel's [pub]/[invite]/[hidden] tag in the
                          # chat status line, distinct from the channel name itself
                          # so the two independent facts (which channel, what kind
                          # of channel) read as separate fields rather than one run
GATE_COLOR = 208  # orange — an access gate a resource carries (a minimum age, a
                  # name requirement) shown as a tag beside its name in a SysOp
                  # resource list. Same reasoning as CHANNEL_TYPE_COLOR, whose
                  # value it shares: "what this thing is called" and "who is
                  # allowed near it" are two independent facts, and a gate that
                  # renders in the same shade as the levels beside it is how a
                  # gated resource came to look identical to an ungated one.
                  # A separate named constant rather than reusing
                  # CHANNEL_TYPE_COLOR directly, matching this module's own
                  # "one constant per meaning" convention.
TOPIC_COLOR = 141  # light purple — the chat status line's quoted channel topic
PRIVILEGE_COLOR = 196  # red — a user's own moderator/SysOp badge ("[mod]",
                       # "[sysop]") in the chat status line, distinct from the
                       # muted gray used for state that's merely informational
                       # (mute expiry, the clock) rather than elevated access
ALERT_COLOR = 202  # deep orange-red — node-wide operational alerts (an active
                   # drain/shutdown countdown, maintenance mode being on) in a
                   # live prompt or a post-login reminder; distinct from
                   # PRIVILEGE_COLOR (an account's own elevated-access badge,
                   # a permanent fact about who's connected) and MUTED_COLOR
                   # (routine informational text) since this specifically
                   # means "something time-sensitive is happening to the node
                   # itself, act on it"
CLOCK_COLOR = 213  # light magenta/orchid — the main-menu prompt's own HH:MM:SS
                   # clock (Thiesi's own explicit follow-up request, after the
                   # clock originally shared HEADER_COLOR with the "Main menu:"
                   # label one line above it and read as part of that header
                   # rather than a separate thing). Distinct from SELF_COLOR
                   # (also magenta family, but specifically "the user's own
                   # chat messages" — an unrelated context that never appears
                   # on the same screen, so the two are kept as separate named
                   # constants rather than one shared value, matching this
                   # module's own "one constant per meaning" convention)
WARNING_COLOR = 214  # amber — a WARNING-level diagnostic log entry (issue #101).
                     # Distinct from ALERT_COLOR, reused here for ERROR/CRITICAL
                     # entries on the same screen: two real severities that need
                     # to read as visually different, not one flat "something's
                     # wrong" color for both.
STATUS_BAR_BACKGROUND = 236  # dark neutral gray — the chat status line's own
                             # solid background band (Thiesi's own explicit
                             # choice: literal per-field foreground colors on
                             # one flat background, not reverse video). Dark
                             # enough to read as a distinct bar against a
                             # typical terminal's own black/near-black
                             # background, neutral enough that it never fights
                             # any of this palette's foreground colors for
                             # attention the way a hue-matched background could

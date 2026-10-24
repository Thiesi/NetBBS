"""SysOp presets (design doc §3.4, issue #1504; decided in #1395).

A preset sets several related values of one area at once: the model
conversation's budgets, Link capacity, and the levels ladder. Each works the
same way, so the shape lives here once:

1. **Choose.** A key on the screen that owns the values opens a short pick
   of the tiers (the levels ladder has one, so it goes straight on).
2. **Preview.** A detail panel lists every value the tier would change as
   `before -> after`, grouped the way the owning screen groups them; values
   already at the target are left out, and values the preset cannot change
   get their own heading with the reason. The tier's warnings and when the
   change takes effect come last.
3. **Confirm.** `[A]pply` on the preview; nothing is written before it.
4. **Apply.** Every value and one `apply_preset` audit entry, naming the
   preset and tier and listing each `key=before→after`, in one transaction.
   The plan is worked out again inside that transaction, so what is written
   and what is audited are the same thing.

A preset is a template, not a profile: the node keeps no record of which one
was applied beyond that audit entry, and every value stays editable on its
own screen. A preset never sets a secret, a host, port or path, identity or
trust material, the node's name, a caller's preferences or the SysOp's own
text, and never switches on anything that sends data off the node.

Each area supplies two synchronous, `db`-first functions: `plan(db, tier)`
says what the tier would change, and `write(db, tier, plan, actor)` writes it
without committing or auditing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Any

from netbbs.auth.users import User
from netbbs.moderation.log import record_action_without_commit
from netbbs.net.location import entered
from netbbs.net.notices import announce
from netbbs.net.session import Session
from netbbs.rendering import MUTED_COLOR, VALUE_COLOR, WARNING_COLOR, colored, menu_key, sanitize_text
from netbbs.rendering.detail import Field, Note, Section, Styled
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

AUDIT_ACTION = "apply_preset"
NOT_CHANGED = "Not changed"


@dataclass(frozen=True)
class PresetChange:
    """One value the tier would change. `value` is what `write` stores."""

    key: str            # named in the audit entry
    label: str          # what the owning screen calls it
    before: str
    after: str
    group: str | None = None
    value: Any = None


@dataclass(frozen=True)
class PresetHeld:
    """A value the preset would set but cannot, and why."""

    label: str
    reason: str


@dataclass(frozen=True)
class PresetPlan:
    changes: tuple[PresetChange, ...] = ()
    held: tuple[PresetHeld, ...] = ()
    warnings: tuple[str, ...] = ()
    # A step the SysOp still has to take outside the console, with the rows
    # to copy (a TOML block): `(heading, sentence, rows)`.
    steps: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    effect: str = ""


@dataclass(frozen=True)
class PresetTier:
    key: str
    label: str
    description: str


@dataclass(frozen=True)
class Preset:
    name: str
    title: str
    tiers: tuple[PresetTier, ...]
    plan: Callable[[Database, PresetTier], PresetPlan]
    write: Callable[[Database, PresetTier, PresetPlan, User], None]
    help_about: str
    # Shown under the pick of tiers.
    pick_preamble: str = ""


def _audit_value(text: str) -> str:
    # Space-separated pairs: a value with a space in it is quoted.
    return json.dumps(text, ensure_ascii=False) if any(char.isspace() for char in text) else text


def audit_detail(preset: Preset, tier: PresetTier, plan: PresetPlan) -> str:
    changes = " ".join(f"{c.key}={_audit_value(c.before)}→{_audit_value(c.after)}" for c in plan.changes)
    return f"preset={preset.name}/{tier.key} {changes}".rstrip()


def apply_preset(db: Database, preset: Preset, tier: PresetTier, *, actor: User) -> PresetPlan:
    """Write `tier` and its audit entry in one transaction, or nothing at
    all. Returns the plan that was applied; one with no changes wrote
    nothing, not even an audit entry."""
    db.connection.execute("BEGIN IMMEDIATE")
    try:
        plan = preset.plan(db, tier)
        if not plan.changes:
            db.connection.rollback()
            return plan
        preset.write(db, tier, plan, actor)
        record_action_without_commit(db, actor=actor, action=AUDIT_ACTION, detail=audit_detail(preset, tier, plan))
    except BaseException:
        db.connection.rollback()
        raise
    db.connection.commit()
    return plan


def preview_sections(plan: PresetPlan, tier: PresetTier, *, unicode_style: bool) -> list[Section]:
    arrow = " → " if unicode_style else " -> "
    sections: list[Section] = []
    if plan.changes:
        groups: dict[str | None, list[Field | Note]] = {}
        for change in plan.changes:
            groups.setdefault(change.group, []).append(Field(
                change.label, sanitize_text(change.before) + arrow + sanitize_text(change.after),
            ))
        sections += [Section(group, rows) for group, rows in groups.items()]
    else:
        sections.append(Section(None, [Note(
            f"Nothing to change: every value {tier.label} sets is already set that way. Nothing will be written.",
            color=VALUE_COLOR,
        )]))
    if plan.held:
        sections.append(Section(NOT_CHANGED, [Field(h.label, h.reason, color=MUTED_COLOR) for h in plan.held]))
    if plan.warnings:
        sections.append(Section("Warnings", [Note(text, color=WARNING_COLOR) for text in plan.warnings]))
    for heading, sentence, rows in plan.steps:
        sections.append(Section(heading, [Note(sentence), Styled([colored("  " + row, fg_color=VALUE_COLOR) for row in rows])]))
    if plan.effect and plan.changes:
        sections.append(Section(None, [Note(plan.effect)]))
    return sections


async def run_preset(session: Session, lane: DatabaseLane, actor: User, preset: Preset) -> None:
    """Choose, preview, confirm and apply `preset`. The outcome is announced
    for the screen this returns to (§3.4); `[B]ack` anywhere writes nothing."""
    from netbbs.net.admin_ui import _BACK_ACTION, _detail_title, _load_chrome, show_detail
    from netbbs.net.menu_description_preference import menu_description_level
    from netbbs.net.picker import pick_item

    with entered(session, preset.title):
        chrome = await _load_chrome(lane, actor)
        tiers = list(preset.tiers)
        while True:
            if len(tiers) == 1:
                tier = tiers[0]
            else:
                tier = await pick_item(
                    session, tiers,
                    name_of=lambda t: t.label,
                    stable_id_of=lambda t: tiers.index(t),
                    description_of=lambda t: t.description,
                    title=preset.title, own=True, empty_message="No presets.",
                    preamble=preset.pick_preamble,
                    description_level=await lane.run(menu_description_level, actor),
                    redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
                    collapsed=chrome.collapsed, header_color=chrome.header_color,
                    accent_color=chrome.accent_color,
                )
                if tier is None:
                    return
            applied = await _preview(session, lane, actor, preset, tier, chrome, show_detail, _detail_title, _BACK_ACTION)
            if applied or len(tiers) == 1:
                return


async def _preview(session, lane, actor, preset, tier, chrome, show_detail, detail_title, back_action) -> bool:
    page = 0
    with entered(session, tier.label) if len(preset.tiers) > 1 else nullcontext():
        while True:
            plan = await lane.run(preset.plan, tier)
            actions = ([("a", menu_key("A", "pply"))] if plan.changes else []) + [back_action]
            # The breadcrumb names the preset; the title names the tier.
            title = tier.label
            choice, page = await show_detail(
                session,
                title=detail_title(session, chrome, title, own=True,
                                   subtitle="What this would change. Nothing is written until you apply it."),
                sections=preview_sections(plan, tier, unicode_style=chrome.unicode_style),
                actions=actions, page=page,
                redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
                help_title=f"{preset.title} help",
                help_about=preset.help_about,
            )
            if choice == "b":
                return False
            try:
                applied = await lane.run(apply_preset, preset, tier, actor=actor)
            except ValueError as exc:
                # Every setter here refuses with a ValueError the SysOp can read.
                announce(session, f"{tier.label} not applied: {exc} Nothing was changed.", tone="error")
                return True
            if not applied.changes:
                announce(session, f"Nothing to change: every value {tier.label} sets is already set that way.",
                         tone="muted")
                return True
            count = len(applied.changes)
            announce(session, f"{tier.label} applied: {count} value{'s' if count != 1 else ''} changed. "
                              f"{applied.effect}")
            return True


def _number(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}" if isinstance(value, int) else str(value)


# -- 1. Model conversation budgets (Decision 6) -------------------------------

MODEL_TIERS = {
    "frugal": {"day_tokens": 25_000, "month_tokens": 250_000, "user_daily": 5, "cooldown": 60,
               "channel_per_minute": 1, "output_tokens": 128, "reply_chars": 300, "context_lines": 2},
    "normal": {"day_tokens": 100_000, "month_tokens": 1_000_000, "user_daily": 20, "cooldown": 10,
               "channel_per_minute": 2, "output_tokens": 256, "reply_chars": 400, "context_lines": 6},
    "generous": {"day_tokens": 1_000_000, "month_tokens": 20_000_000, "user_daily": 100, "cooldown": 5,
                 "channel_per_minute": 6, "output_tokens": 512, "reply_chars": 500, "context_lines": 10},
}


def _model_plan(db: Database, tier: PresetTier) -> PresetPlan:
    from netbbs.bots.model import FIELD_LABELS, settings

    current = settings(db)
    targets = MODEL_TIERS[tier.key]
    changes = tuple(
        PresetChange(key, FIELD_LABELS[key], _number(getattr(current, key)), _number(target),
                     group="Privacy and access" if key == "context_lines" else "Limits", value=target)
        for key, target in targets.items() if getattr(current, key) != target
    )
    # The owning editor's order: Privacy and access before Limits.
    changes = tuple(sorted(changes, key=lambda c: c.group != "Privacy and access"))
    warnings = []
    raises = any(targets[key] > getattr(current, key) for key in ("day_tokens", "month_tokens"))
    if raises and not current.day_money and not current.month_money:
        warnings.append(
            "This raises the token budgets, and no money budget is set: nothing but the token counts caps "
            "what a paid provider charges. Set prices and a money budget under Model settings if you pay "
            "for tokens."
        )
    if not current.enabled:
        warnings.append("Model conversation is off. These limits apply once it is switched on; this preset leaves it off.")
    return PresetPlan(
        changes=changes,
        held=(PresetHeld("Prices and money budgets", "yours to set, in your own currency"),),
        warnings=tuple(warnings),
        effect="Changes apply from the next request.",
    )


def _model_write(db: Database, tier: PresetTier, plan: PresetPlan, actor: User) -> None:
    from netbbs.bots.model import save_settings_without_commit, settings

    # The stored API key is left exactly as it is (`api_key=None`).
    value = replace(settings(db), **{change.key: change.value for change in plan.changes})
    save_settings_without_commit(db, value, actor=actor)


MODEL_BUDGETS = Preset(
    name="model_budgets",
    title="Budget presets",
    tiers=(
        PresetTier("frugal", "Frugal", "25,000 tokens a day, 5 requests per caller, short replies"),
        PresetTier("normal", "Normal", "The defaults: 100,000 tokens a day, 20 requests per caller"),
        PresetTier("generous", "Generous", "1,000,000 tokens a day, 100 requests per caller, long replies"),
    ),
    plan=_model_plan, write=_model_write,
    pick_preamble="Sets the model conversation's token budgets, rate limits, reply length and prior "
                  "exchange lines. Prices, money budgets, the provider and every other privacy setting "
                  "stay as they are.",
    help_about="Each value this tier would change, as before -> after; values already set that way are "
               "left out: token budgets, requests per caller, cooldown, channel rate, output tokens, reply length "
               "and prior exchange lines. Apply writes them all, used from the next request, with one audit "
               "entry; Back writes nothing. Prices, money budgets and the provider stay as they are.",
)


# -- 2. Link capacity (Decision 7) --------------------------------------------

CAPACITY_KEYS = (
    "link.max_carried_boards", "link.max_carried_channels", "link.max_carried_file_areas",
    "link.max_peers", "link.relay_serving_enabled", "link.max_relay_clients",
    "link.max_remote_files_per_area", "link.request_rate_capacity", "link.request_rate_refill_per_minute",
    "link.diagnostic_log_max_age_days", "link.diagnostic_log_max_rows",
    "link.live_relay_max_concurrent_pairs", "link.live_relay_max_pending_rendezvous",
    "link.live_relay_max_bytes_per_second",
)
# `None` for Default: forget the stored value, so the built-in default -- and
# any later change to it -- applies. A key a tier leaves out stays as it is.
CAPACITY_TIERS: dict[str, dict[str, Any] | None] = {
    "small": {
        "link.max_carried_boards": 50, "link.max_carried_channels": 50, "link.max_carried_file_areas": 50,
        "link.max_peers": 200, "link.max_remote_files_per_area": 1_000,
        "link.diagnostic_log_max_age_days": 14, "link.diagnostic_log_max_rows": 2_000,
    },
    "default": None,
    "hub": {
        "link.max_carried_boards": 2_000, "link.max_carried_channels": 2_000, "link.max_carried_file_areas": 2_000,
        "link.max_peers": 5_000, "link.relay_serving_enabled": True, "link.max_relay_clients": 100,
        "link.max_remote_files_per_area": 20_000,
        "link.request_rate_capacity": 60.0, "link.request_rate_refill_per_minute": 180.0,
        "link.diagnostic_log_max_age_days": 90, "link.diagnostic_log_max_rows": 20_000,
        "link.live_relay_max_concurrent_pairs": 32, "link.live_relay_max_pending_rendezvous": 128,
        "link.live_relay_max_bytes_per_second": 262_144,
    },
}
SET_IN_CONFIG = "set in config — not changed"
FULL_PEER_TOML = (
    "[link]",
    "outgoing_only = false",
    'advertised_host = "bbs.example.org"',
    "advertised_port = 7862",
    "realtime_advertised_port = 8862",
)


def _outgoing_only(db: Database) -> bool | None:
    """Whether the node last started outgoing-only, or `None` if unknown."""
    from netbbs.net.bootstrap_view import load_bootstrap_snapshot

    rows = load_bootstrap_snapshot(db)
    if rows is None:
        return None
    row = next((r for r in rows if r.key == "link.outgoing_only"), None)
    return None if row is None else row.value.startswith("yes")


def _capacity_plan(db: Database, tier: PresetTier) -> PresetPlan:
    from netbbs.net.nodeconfig import NodeConfig
    from netbbs.net.policy_settings import format_value, load_policy_views

    views, started = load_policy_views(db, NodeConfig())
    by_key = {view.setting.key: view for view in views}
    targets = CAPACITY_TIERS[tier.key]
    changes, held = [], []
    for key in CAPACITY_KEYS:
        view = by_key[key]
        setting = view.setting
        if targets is not None and key not in targets:
            continue
        if view.overridden:
            held.append(PresetHeld(setting.label, SET_IN_CONFIG))
            continue
        current = view.stored if view.stored is not None else view.default
        if targets is None:
            if view.stored is None:
                continue
            after = f"default ({format_value(setting, view.default)})"
            changes.append(PresetChange(key, setting.label, format_value(setting, current), after,
                                        group=setting.group, value=None))
            continue
        target = targets[key]
        if current == target:
            continue
        changes.append(PresetChange(key, setting.label, format_value(setting, current), format_value(setting, target),
                                    group=setting.group, value=target))
    warnings = []
    steps = []
    if not started:
        warnings.append("The node has not started since this version, so which settings the config file holds "
                        "is not known yet. A value the config file sets will still win.")
    if tier.key == "hub":
        warnings.append("Higher carry caps let other nodes fill this node's storage with what they offer.")
        warnings.append("Relaying spends this node's bandwidth on nodes you do not know.")
        outgoing = _outgoing_only(db)
        if outgoing is not False:
            lead = ("This node is outgoing-only" if outgoing else
                    "If this node is outgoing-only (the default)")
            warnings.append(f"{lead}, relay serving does nothing: only a full peer relays.")
            steps.append((
                "Still to do",
                "A preset cannot make this node a full peer. To become one, set this in the config file "
                "with your own host name, forward both ports to this node, and restart:",
                FULL_PEER_TOML,
            ))
    return PresetPlan(changes=tuple(changes), held=tuple(held), warnings=tuple(warnings), steps=tuple(steps),
                      effect="Changes apply the next time the node starts.")


def _capacity_write(db: Database, tier: PresetTier, plan: PresetPlan, actor: User) -> None:
    from netbbs.net.policy_settings import save_policy_without_commit

    save_policy_without_commit(db, {change.key: change.value for change in plan.changes})


LINK_CAPACITY = Preset(
    name="link_capacity",
    title="Capacity presets",
    tiers=(
        PresetTier("small", "Small", "50 of each carried, 200 peers, two weeks of diagnostics"),
        PresetTier("default", "Default", "Forgets the values saved here: the built-in defaults apply"),
        PresetTier("hub", "Hub", "Carries 2,000 of each, 5,000 peers, relays for 100 nodes"),
    ),
    plan=_capacity_plan, write=_capacity_write,
    pick_preamble="Sets the Link carry caps, peering, Link limits and real-time relay at once. The login "
                  "throttle, shutdown delays, seeds and sync interval stay as they are.",
    help_about="Each Link value this tier would change, as before -> after; one the config file or command "
               "line sets is listed under Not changed and left alone. Apply writes them all, with one audit "
               "entry, for the next start; Back writes nothing. Every value stays editable under its group.",
)


# -- 3. Levels ladder (Decision 8) --------------------------------------------

LADDER_NAMES = ((0, "New"), (10, "Member"))
LADDER_RULE_FROM, LADDER_RULE_TO, LADDER_RULE_HOURS, LADDER_RULE_LOGINS = 0, 10, 24, 2
LADDER_LEVEL = 10


def _rule_text(rule) -> str:
    return f"{rule.from_level} to {rule.to_level} after {rule.describe()}" if rule is not None else "(none)"


_HELD_REASONS = {"the guest account": "guest"}


def held_back(db: Database) -> dict[str, int]:
    """Level-0 accounts the ladder's rule never raises, by why
    (`promotion.kept_from_rules`)."""
    from netbbs.auth.users import get_user_by_id
    from netbbs.promotion import kept_from_rules

    counts: dict[str, int] = {}
    for (user_id,) in db.connection.execute("SELECT id FROM users WHERE user_level = ?", (LADDER_RULE_FROM,)):
        user = get_user_by_id(db, user_id)
        reason = kept_from_rules(db, user) if user is not None else None
        if reason is not None:
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def _ladder_plan(db: Database, tier: PresetTier) -> PresetPlan:
    from netbbs.config import get_mail_min_level, get_oneliner_min_level
    from netbbs.level_names import get_level_names, level_name_holder
    from netbbs.promotion import PromotionRule, get_promotion_rules

    names = get_level_names(db)
    changes, held = [], []
    for level, name in LADDER_NAMES:
        current = names.get(level)
        if current == name:
            continue
        label = f"Name of level {level}"
        taken = level_name_holder(names, level, name)
        if taken is not None:
            held.append(PresetHeld(label, f"level {taken} is already called {names[taken]}"))
            continue
        changes.append(PresetChange(f"level_name.{level}", label, current or "(none)", name,
                                    group="Level names", value=name))
    target = PromotionRule(LADDER_RULE_FROM, LADDER_RULE_TO, min_age_hours=LADDER_RULE_HOURS,
                           min_logins=LADDER_RULE_LOGINS)
    existing = next((r for r in get_promotion_rules(db) if r.from_level == LADDER_RULE_FROM), None)
    if existing != target:
        changes.append(PresetChange(f"promotion_rule.{LADDER_RULE_FROM}", f"Rule from level {LADDER_RULE_FROM}",
                                    _rule_text(existing), _rule_text(target), group="Auto-promotion rules",
                                    value=target))
    gates_raised = False
    for key, label, current in (("mail_min_level", "Mail level", get_mail_min_level(db)),
                                ("oneliner_min_level", "Oneliner level", get_oneliner_min_level(db))):
        if current != LADDER_LEVEL:
            gates_raised = gates_raised or current < LADDER_LEVEL
            changes.append(PresetChange(key, label, str(current), str(LADDER_LEVEL), group="Limits", value=LADDER_LEVEL))
    warnings = []
    if gates_raised:
        warnings.append(
            f"Accounts at level {LADDER_RULE_FROM} can no longer use mail or add oneliners until the rule raises "
            f"them, at their next login once they qualify."
        )
    counts = held_back(db)
    if counts:
        total = sum(counts.values())
        why = ", ".join(f"{n} {_HELD_REASONS.get(reason, reason)}" for reason, n in sorted(counts.items()))
        warnings.append(
            f"{total} account{'s' if total != 1 else ''} at level {LADDER_RULE_FROM} the rule never raises ({why}). "
            f"Raise {'them' if total != 1 else 'it'} by hand under Users > Promote/demote if "
            f"{'they' if total != 1 else 'it'} should keep mail and oneliners."
        )
    return PresetPlan(changes=tuple(changes), held=tuple(held), warnings=tuple(warnings),
                      effect="Changes apply at once.")


def _ladder_write(db: Database, tier: PresetTier, plan: PresetPlan, actor: User) -> None:
    from netbbs.config import MAIL_MIN_LEVEL_CONFIG_KEY, ONELINER_MIN_LEVEL_CONFIG_KEY, set_config_without_commit
    from netbbs.level_names import set_level_name_without_commit
    from netbbs.promotion import get_promotion_rules, save_promotion_rules_without_commit

    config_keys = {"mail_min_level": MAIL_MIN_LEVEL_CONFIG_KEY, "oneliner_min_level": ONELINER_MIN_LEVEL_CONFIG_KEY}
    for change in plan.changes:
        if change.key.startswith("level_name."):
            set_level_name_without_commit(db, int(change.key.split(".", 1)[1]), change.value)
        elif change.key.startswith("promotion_rule."):
            # Only the rule from level 0 is replaced; every other rule stays.
            others = [r for r in get_promotion_rules(db) if r.from_level != change.value.from_level]
            save_promotion_rules_without_commit(db, [*others, change.value])
        else:
            set_config_without_commit(db, config_keys[change.key], str(change.value))


LEVELS_LADDER = Preset(
    name="levels_ladder",
    title="Levels ladder",
    tiers=(PresetTier("standard", "Levels ladder", "New at 0, Member at 10 after a day and 2 logins"),),
    plan=_ladder_plan, write=_ladder_write,
    help_about="Names level 0 New and level 10 Member, raises accounts from 0 to 10 at their first login "
               "after 24 hours and 2 logins, and opens mail and oneliners at 10. The preview lists each "
               "change as before -> after; Apply writes them at once, Back writes nothing. Other names and "
               "rules stay as they are.",
)


PRESETS: Sequence[Preset] = (MODEL_BUDGETS, LINK_CAPACITY, LEVELS_LADDER)

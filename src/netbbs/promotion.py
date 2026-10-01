"""
Automatic level promotion (design doc §4.3, issue #992): rules a SysOp sets,
each raising an account from one level to the next once it is old enough,
has logged in often enough and, optionally, has posted enough.

Rules are checked when an account logs in, before its session takes its
level, so the session starts at the new level and the caller is told. An
account that qualifies while away is promoted at its next login, the first
time the level matters. Nothing runs on a timer.

A rule never demotes and never reaches 255. It leaves alone the guest
account, pending and disabled accounts, staff and SysOps, and any account
whose level a person has set: a SysOp's demotion is not undone at the next
login. One rule applies per login, so an account climbs a ladder one step
per call.

Rules are node configuration, kept as one JSON list (as `level_names` is),
and travel in backups.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import asdict, dataclass

from netbbs.auth.users import SYSOP_LEVEL, User, promote_automatically
from netbbs.config import get_config, set_config
from netbbs.storage.database import Database
from netbbs.timeutil import parse_utc_iso

PROMOTION_RULES_CONFIG_KEY = "promotion_rules"
MAX_RULE_AGE_HOURS = 24 * 365
MAX_RULE_COUNT = 100_000


class PromotionRuleError(ValueError):
    """A rule, or a set of rules, that cannot be saved, with why."""


@dataclass(frozen=True)
class PromotionRule:
    from_level: int
    to_level: int
    min_age_hours: int = 0
    min_logins: int = 0
    min_posts: int = 0

    def describe(self) -> str:
        """What an account needs, in a few words: `24h, 2 logins, 1 post`."""
        parts = []
        if self.min_age_hours:
            parts.append(f"{self.min_age_hours}h")
        if self.min_logins:
            parts.append(f"{self.min_logins} login{'s' if self.min_logins != 1 else ''}")
        if self.min_posts:
            parts.append(f"{self.min_posts} post{'s' if self.min_posts != 1 else ''}")
        return ", ".join(parts) or "at the next login"


def get_promotion_rules(db: Database) -> list[PromotionRule]:
    """The node's rules, lowest `from_level` first. A stored value that
    cannot be read is ignored, rule by rule, rather than stopping logins."""
    try:
        stored = json.loads(get_config(db, PROMOTION_RULES_CONFIG_KEY) or "[]")
    except ValueError:
        return []
    rules = []
    for item in stored if isinstance(stored, list) else []:
        try:
            rule = PromotionRule(**{key: int(item[key]) for key in item})
            validate_rule(rule)
        except (TypeError, ValueError, KeyError):
            continue
        rules.append(rule)
    return sorted(rules, key=lambda rule: rule.from_level)


def validate_rule(rule: PromotionRule) -> PromotionRule:
    if not 0 <= rule.from_level < rule.to_level:
        raise PromotionRuleError("A rule raises an account: its new level must be above the one it starts from.")
    if rule.to_level >= SYSOP_LEVEL:
        raise PromotionRuleError(f"A rule goes up to {SYSOP_LEVEL - 1} at most; only a SysOp makes a SysOp.")
    if not 0 <= rule.min_age_hours <= MAX_RULE_AGE_HOURS:
        raise PromotionRuleError(f"The account age runs from 0 to {MAX_RULE_AGE_HOURS} hours.")
    for value, what in ((rule.min_logins, "logins"), (rule.min_posts, "posts")):
        if not 0 <= value <= MAX_RULE_COUNT:
            raise PromotionRuleError(f"The number of {what} runs from 0 to {MAX_RULE_COUNT}.")
    return rule


def save_promotion_rules(db: Database, rules: list[PromotionRule], *, changed_by: User) -> list[PromotionRule]:
    """Replace the node's rules. Raises `PromotionRuleError` for a rule that
    cannot be used, or two rules starting from the same level (which one
    would apply would be a guess)."""
    from netbbs.moderation.log import record_action

    for rule in rules:
        validate_rule(rule)
    starts = [rule.from_level for rule in rules]
    clash = next((level for level in starts if starts.count(level) > 1), None)
    if clash is not None:
        raise PromotionRuleError(f"Two rules start from level {clash}; one level, one rule.")
    ordered = sorted(rules, key=lambda rule: rule.from_level)
    set_config(db, PROMOTION_RULES_CONFIG_KEY, json.dumps([asdict(rule) for rule in ordered]))
    record_action(
        db, actor=changed_by, action="promotion_rules",
        detail="; ".join(f"{r.from_level} -> {r.to_level} ({r.describe()})" for r in ordered) or "no rules",
    )
    return ordered


def _is_designated_guest(db: Database, user: User) -> bool:
    """The guest login's account, whether guest login is on or not: it logs
    in constantly and would always qualify."""
    from netbbs.guest import guest_designation

    designation = guest_designation(db)
    return designation is not None and designation == (user.id, user.created_at)


def _approved_posts(db: Database, user: User) -> int:
    """Approved posts this account wrote here, each counted once however
    often it was edited."""
    return db.connection.execute(
        "SELECT COUNT(*) FROM posts WHERE author_user_id = ? AND status = 'approved' AND edit_of_post_id IS NULL",
        (user.id,),
    ).fetchone()[0]


def kept_from_rules(db: Database, user: User) -> str | None:
    """Why the rules leave `user` alone, or `None` when they may promote it."""
    if user.user_level >= SYSOP_LEVEL:
        return "SysOp"
    if user.staff_permissions:
        return "staff"
    if user.pending_approval:
        return "awaiting approval"
    if user.disabled_at is not None:
        return "disabled"
    if _is_designated_guest(db, user):
        return "the guest account"
    if user.level_set_by_hand:
        return "level set by hand"
    return None


def qualifies(
    db: Database, user: User, rule: PromotionRule, *, now: datetime.datetime | None = None,
    next_login: bool = False,
) -> bool:
    """Whether `rule` would promote `user` now, at a login already counted.
    With `next_login`, whether it would at the account's next login, which
    that login's own count still has to reach."""
    if user.user_level != rule.from_level or kept_from_rules(db, user) is not None:
        return False
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now - parse_utc_iso(user.created_at) < datetime.timedelta(hours=rule.min_age_hours):
        return False
    if user.login_count + (1 if next_login else 0) < rule.min_logins:
        return False
    return rule.min_posts == 0 or _approved_posts(db, user) >= rule.min_posts


def promote_at_login(db: Database, user: User, *, now: datetime.datetime | None = None) -> User | None:
    """Apply the rule for `user`'s level if it qualifies; return the account
    as promoted, or `None`. Called once per login, after the login is
    counted. One step per login."""
    from netbbs.auth.users import get_user_by_id

    # Read again: the caller's copy predates this login's count.
    user = get_user_by_id(db, user.id) or user
    rule = next((r for r in get_promotion_rules(db) if r.from_level == user.user_level), None)
    if rule is None or not qualifies(db, user, rule, now=now):
        return None
    return promote_automatically(db, user, rule.from_level, rule.to_level, reason=f"rule: {rule.describe()}")


def count_qualifying(db: Database, rule: PromotionRule, *, now: datetime.datetime | None = None) -> int:
    """Accounts the rule would promote at their next login, for the console."""
    from netbbs.auth.users import get_user_by_id

    ids = [row[0] for row in db.connection.execute(
        "SELECT id FROM users WHERE user_level = ? AND level_set_by_hand = 0", (rule.from_level,)
    )]
    return sum(
        1 for user_id in ids
        if (user := get_user_by_id(db, user_id)) and qualifies(db, user, rule, now=now, next_login=True)
    )

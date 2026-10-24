"""
A board poll on screen (issue #1299, built by #1332): the draft editor that
sets one up, the block the post reader shows under the post, and the vote.

- **The editor** is a draft (`edit_resource_draft`, design doc §3.5): the
  question, up to ten answers, how many a voter may pick, when it closes
  and when results show. Nothing is written by it; `[N]ext` hands the draft
  back, and the post is written at the review screen's `[P]ost`.
- **The block** shows the results as bars once the caller may see them --
  after voting, or at once when they cannot vote, or only after the close
  when the author chose that -- and otherwise says when they will. It never
  names a voter: totals and the number of voters only.
- **The vote** is a draft of ticks over the answers, `[V]ote` storing it.
"""

from __future__ import annotations

import datetime
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.boards.boards import Board
from netbbs.boards.limits import MAX_SUBJECT_BYTES
from netbbs.boards.polls import (
    DEFAULT_POLL_DAYS,
    MAX_POLL_DAYS,
    MAX_POLL_OPTIONS,
    MIN_POLL_OPTIONS,
    RESULTS_AFTER_CLOSE,
    RESULTS_AFTER_VOTE,
    Poll,
    PollError,
    PollSpec,
    PollTally,
    cast_vote,
    closing_moment,
    default_closes_at,
    validate_poll_spec,
)
from netbbs.net.char_input import InputCancelled
from netbbs.net.resource_editor import (
    Draft,
    FieldSpec,
    edit_resource_draft,
    inline_field,
    read_field_line,
    text_field,
    write_field_message,
    write_field_prompt,
)
from netbbs.net.session import Session
from netbbs.rendering import (
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    colored,
    cut_to_width,
    display_width,
    menu_key,
    sanitize_text,
    wrap_to_width,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, parse_utc_iso, utc_now_iso

# One row per answer from this width up; two rows below it (design doc,
# issue #1299 Decision 3).
WIDE_POLL_WIDTH = 60
_MIN_BAR = 8
_MAX_BAR = 30
# Below this height a draft editor's explanatory line gives its row to the
# fields.
_PREAMBLE_MIN_HEIGHT = 20

LINKED_VOTE_NOTICE = "Your vote is shared with every BBS that carries this board."

_RESULTS_LABELS = {
    RESULTS_AFTER_VOTE: "After voting",
    RESULTS_AFTER_CLOSE: "Only after the poll closes",
}


def choices_phrase(max_choices: int) -> str:
    return "Pick one answer" if max_choices == 1 else f"Pick up to {max_choices} answers"


def _days_until(closes_at: str) -> int:
    remaining = parse_utc_iso(closes_at) - parse_utc_iso(utc_now_iso())
    return max(0, math.ceil(remaining / datetime.timedelta(days=1)))


def _closing_text(db: Database, closes_at: str) -> str:
    days = _days_until(closes_at)
    when = format_for_display(closes_at, db)
    if days <= 0:
        return when
    return f"{when} (in {days} day{'s' if days != 1 else ''})"


# -- the results --------------------------------------------------------------


def _percent(count: int, voters: int) -> int:
    return round(count * 100 / voters) if voters else 0


def result_rows(
    poll: Poll,
    tally: PollTally,
    *,
    width: int,
    unicode_style: bool,
    accent,
    mine: tuple[int, ...] | None = None,
) -> list[str]:
    """The answers as bars: one row per answer at `WIDE_POLL_WIDTH` columns
    or more -- the label (wrapping under itself), the bar, the count and the
    percentage of voters -- and two rows each below it. The caller's own
    answers are marked. Every row fits `width - 1` columns."""
    usable = max(10, width - 1)
    full, empty = ("█", "░") if unicode_style else ("#", ".")
    mark = "✓" if unicode_style else "*"
    mine_set = set(mine or ())
    count_width = max(len(str(c)) for c in tally.counts) if tally.counts else 1
    tail_width = 2 + count_width + 2 + 4  # "  12  100%"
    rows: list[str] = []
    labels = [sanitize_text(option).replace("\t", " ") for option in poll.options]

    def _bar(count: int, bar_width: int) -> str:
        filled = round(bar_width * count / tally.voters) if tally.voters else 0
        return colored(full * filled, fg_color=accent) + colored(empty * (bar_width - filled), fg_color=MUTED_COLOR)

    def _tail(count: int) -> str:
        return colored(f"  {count:>{count_width}}  {_percent(count, tally.voters):>3}%", fg_color=METADATA_COLOR)

    if usable >= WIDE_POLL_WIDTH:
        label_width = min(max(display_width(label) for label in labels), max(12, (usable - 2) * 2 // 5))
        bar_width = max(_MIN_BAR, min(_MAX_BAR, usable - 2 - label_width - 2 - tail_width))
        for index, (label, count) in enumerate(zip(labels, tally.counts)):
            lead = f"{mark} " if index in mine_set else "  "
            wrapped = wrap_to_width(label, label_width) or [""]
            first = wrapped[0] + " " * (label_width - display_width(wrapped[0]))
            rows.append(colored(lead, fg_color=accent) + first + "  " + _bar(count, bar_width) + _tail(count))
            rows.extend("  " + continuation for continuation in wrapped[1:])
        return rows

    bar_width = max(4, min(_MAX_BAR, usable - 2 - tail_width))
    for index, (label, count) in enumerate(zip(labels, tally.counts)):
        lead = f"{mark} " if index in mine_set else "  "
        wrapped = wrap_to_width(label, usable - 2) or [""]
        rows.append(colored(lead, fg_color=accent) + wrapped[0])
        rows.extend("  " + continuation for continuation in wrapped[1:])
        rows.append("  " + _bar(count, bar_width) + _tail(count))
    return rows


def answer_rows(poll: Poll | PollSpec, *, width: int, accent) -> list[str]:
    """The answers as a numbered list, without results."""
    usable = max(10, width - 1)
    rows: list[str] = []
    for index, option in enumerate(poll.options):
        number = f"{index + 1:>3} "
        wrapped = wrap_to_width(sanitize_text(option).replace("\t", " "), usable - len(number)) or [""]
        rows.append(colored(number, fg_color=accent) + wrapped[0])
        rows.extend(" " * len(number) + continuation for continuation in wrapped[1:])
    return rows


@dataclass(frozen=True)
class PollView:
    """What one caller is shown of a poll, worked out by the reader."""

    poll: Poll
    tally: PollTally
    mine: tuple[int, ...] | None
    closed: bool
    approved: bool
    hidden: str | None  # "withdrawn" / "removed" when its totals are hidden
    can_vote: bool
    linked: bool = False

    @property
    def shows_results(self) -> bool:
        if self.hidden is not None:
            return False
        if self.poll.results == RESULTS_AFTER_CLOSE:
            return self.closed
        return self.closed or self.mine is not None or not self.can_vote


def _wrapped_note(text: str, width: int, color=MUTED_COLOR) -> list[str]:
    return [colored(row, fg_color=color) for row in wrap_to_width(text, max(10, width - 1))]


def poll_block_rows(db: Database, view: PollView, *, width: int, unicode_style: bool, accent) -> list[str]:
    """The block the reader shows under a poll's post: how to vote and when
    it closes, the results or the answers, and what the caller should
    know -- when results show, that a held poll waits, that a Linked vote
    is shared."""
    poll = view.poll
    rows: list[str] = []
    if view.hidden is not None:
        rows.extend(_wrapped_note(
            f"This poll closed when its post was {view.hidden}. Its results are hidden.", width,
        ))
        return rows
    if view.closed:
        status = f"Closed {format_for_display(closing_moment(poll), db)}"
    else:
        status = f"Closes {_closing_text(db, poll.closes_at)}"
    rows.extend(_wrapped_note(f"{choices_phrase(poll.max_choices)}. {status}.", width, METADATA_COLOR))
    if view.shows_results:
        rows.extend(result_rows(
            poll, view.tally, width=width, unicode_style=unicode_style, accent=accent, mine=view.mine,
        ))
        summary = f"{view.tally.voters} voter{'s' if view.tally.voters != 1 else ''}"
        if view.tally.not_counted:
            summary += f", {view.tally.not_counted} vote{'s' if view.tally.not_counted != 1 else ''} not counted"
        rows.extend(_wrapped_note(summary + ".", width, METADATA_COLOR))
    else:
        rows.extend(answer_rows(poll, width=width, accent=accent))
        if view.mine is not None:
            picked = ", ".join(sanitize_text(poll.options[i]) for i in view.mine)
            rows.extend(_wrapped_note(f"Your vote: {picked}.", width, LABEL_COLOR))
        if poll.results == RESULTS_AFTER_CLOSE:
            rows.extend(_wrapped_note(
                f"Results show when the poll closes, {format_for_display(poll.closes_at, db)}.", width,
            ))
        else:
            rows.extend(_wrapped_note("Results show once you vote.", width))
    if not view.approved:
        rows.extend(_wrapped_note("Voting opens once a moderator approves this poll.", width))
    elif view.can_vote and view.linked and view.mine is None:
        rows.extend(_wrapped_note(LINKED_VOTE_NOTICE, width))
    return rows


def review_rows(spec: PollSpec, *, db: Database, width: int, accent) -> list[str]:
    """The poll as the review screen shows it before posting."""
    inner = max(10, width - 2)
    rows = [colored("Poll:", fg_color=LABEL_COLOR)]
    rows.extend(answer_rows(spec, width=inner, accent=accent))
    rows.extend(_wrapped_note(
        f"{choices_phrase(spec.max_choices)}. Closes {_closing_text(db, spec.closes_at)}. "
        f"Results: {_RESULTS_LABELS[spec.results].lower()}.",
        inner,
    ))
    return ["  " + row for row in rows]


# -- the draft editor -----------------------------------------------------------


def _answer_key(index: int) -> str:
    return f"answer{index}"


def _draft_options(draft: Draft) -> tuple[str, ...]:
    return tuple(
        draft[_answer_key(i)].strip() for i in range(MAX_POLL_OPTIONS) if (draft.get(_answer_key(i)) or "").strip()
    )


def _choices_render(draft: Draft) -> str:
    return "One answer" if draft["max_choices"] == 1 else f"Up to {draft['max_choices']} answers"


def _choices_values(draft: Draft) -> list[int]:
    return list(range(1, max(MIN_POLL_OPTIONS, len(_draft_options(draft))) + 1))


async def _choices_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
    _choices_step(draft, 1)


def _choices_step(draft: Draft, direction: int) -> None:
    values = _choices_values(draft)
    current = draft["max_choices"] if draft["max_choices"] in values else values[-1]
    draft["max_choices"] = values[(values.index(current) + direction) % len(values)]


def _closes_step(draft: Draft, direction: int) -> None:
    days = min(MAX_POLL_DAYS, max(1, _days_until(draft["closes_at"]) + direction))
    draft["closes_at"] = default_closes_at(days)


@inline_field
async def _closes_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
    await write_field_prompt(
        session, colored(f"Close in how many days (1-{MAX_POLL_DAYS}):", fg_color=MUTED_COLOR),
        hint=f"Days, 1-{MAX_POLL_DAYS}; Enter sets; Esc keeps",
    )
    try:
        raw = (await read_field_line(session, initial=str(_days_until(draft["closes_at"])))).strip()
    except InputCancelled:
        await session.write_line("")
        return
    if raw.isascii() and raw.isdigit() and 1 <= int(raw) <= MAX_POLL_DAYS:
        draft["closes_at"] = default_closes_at(int(raw))
    else:
        await write_field_message(
            session, colored(f"Unchanged: a number of days from 1 to {MAX_POLL_DAYS}.", fg_color=MUTED_COLOR),
        )


async def _results_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
    _results_step(draft, 1)


def _results_step(draft: Draft, direction: int) -> None:
    draft["results"] = RESULTS_AFTER_CLOSE if draft["results"] == RESULTS_AFTER_VOTE else RESULTS_AFTER_VOTE


@dataclass(frozen=True)
class PollDraftResult:
    question: str
    spec: PollSpec


@dataclass(frozen=True)
class ReviewPoll:
    """A poll on the post review screen: `rows(width)` shows it, and
    `edit(subject)` reopens its draft editor from `[Q]uestion and answers`, returning the
    question -- the post's subject -- as it now stands."""

    rows: Callable[[int], list[str]]
    edit: Callable[[str], Awaitable[str]]


REVIEW_POLL_ACTION = ("q", menu_key("Q", "uestion and answers"), "Change the poll and its settings")


async def edit_poll_draft(
    session: Session,
    db: Database,
    *,
    title: str,
    question: str = "",
    spec: PollSpec | None = None,
    answers_locked: str | None = None,
    closing_locked: str | None = None,
    chrome: dict,
) -> PollDraftResult | None:
    """The poll draft editor: the question, 2 to 10 answers, one answer or
    up to N, the closing date (7 days ahead by default) and when results
    show. Returns the draft on `[N]ext`, or `None` on `[B]ack`; nothing is
    stored here. `answers_locked`/`closing_locked`, when given, say why
    those fields cannot change on a poll being edited."""
    draft: Draft = {
        "question": question,
        "max_choices": spec.max_choices if spec is not None else 1,
        "closes_at": spec.closes_at if spec is not None else default_closes_at(DEFAULT_POLL_DAYS),
        "results": spec.results if spec is not None else RESULTS_AFTER_VOTE,
    }
    for index in range(MAX_POLL_OPTIONS):
        draft[_answer_key(index)] = spec.options[index] if spec is not None and index < len(spec.options) else ""

    fields = [
        FieldSpec(
            "question", "Question", lambda d: d["question"] or "(none)", text_field("question", required=True),
            brief="The question, which is the post's subject", section="Poll",
            help="What the poll asks. It is the post's subject on the board's list.",
        ),
    ]
    for index in range(MAX_POLL_OPTIONS):
        key = _answer_key(index)
        fields.append(FieldSpec(
            key, f"Answer {index + 1}", lambda d, key=key: d[key] or "(none)", text_field(key),
            brief="An answer; leave blank for none",
            help=f"Between {MIN_POLL_OPTIONS} and {MAX_POLL_OPTIONS} answers. A blank answer is left out.",
            section="Poll", locked=answers_locked,
        ))
    fields.extend([
        FieldSpec(
            "max_choices", "Choices", _choices_render, _choices_prompt, step=_choices_step,
            brief="One answer, or up to N", section="Settings", locked=answers_locked,
            help="How many answers a voter may pick: one, or up to a number you choose.",
        ),
        FieldSpec(
            "closes_at", "Closes", lambda d: _closing_text(db, d["closes_at"]), _closes_prompt, step=_closes_step,
            brief="When voting ends", section="Settings", locked=closing_locked,
            help=(
                f"Voting ends this many days from now, 1 to {MAX_POLL_DAYS}, {DEFAULT_POLL_DAYS} by default. "
                "You or a moderator can close it sooner from the post."
            ),
        ),
        FieldSpec(
            "results", "Results show", lambda d: _RESULTS_LABELS[d["results"]], _results_prompt, step=_results_step,
            brief="After voting, or only after it closes", section="Settings", locked=answers_locked,
            help="After voting: a caller sees the results once they have voted, or at once if they cannot "
                 "vote. Only after the poll closes: nobody sees them before then. Results are totals; "
                 "nobody sees who voted for what.",
        ),
    ])

    async def _save(saved: Draft) -> PollDraftResult:
        text = " ".join((saved["question"] or "").split())
        if not text:
            raise PollError("the poll needs a question")
        if len(text.encode("utf-8")) > MAX_SUBJECT_BYTES:
            raise PollError(f"the question is too long (max {MAX_SUBJECT_BYTES} bytes)")
        options = _draft_options(saved)
        result = PollSpec(
            options=options, max_choices=min(saved["max_choices"], max(1, len(options))),
            closes_at=saved["closes_at"], results=saved["results"],
        )
        if spec is None or result != spec:
            # An edit that changes nothing is not judged again: a closed
            # poll's closing date is in the past.
            validate_poll_spec(result)
        return PollDraftResult(text, result)

    lane = DatabaseLane(db.path)
    try:
        return await edit_resource_draft(
            session, lane, title=title, fields=fields, draft=draft, save=_save, error_type=PollError,
            save_menu_text=menu_key("N", "ext"), save_hotkey="n", back_menu_text=menu_key("B", "ack"), own=True,
            save_brief="Write the post that introduces the poll",
            # A short terminal keeps its rows for the fields.
            preamble=(
                colored("Set up the poll; the post that introduces it comes next.", fg_color=MUTED_COLOR)
                if session.terminal_height >= _PREAMBLE_MIN_HEIGHT else None
            ),
            **chrome,
        )
    finally:
        lane.close()


# -- voting -----------------------------------------------------------------


async def vote_on_poll(
    session: Session,
    db: Database,
    user: User,
    poll: Poll,
    board: Board,
    *,
    mine: tuple[int, ...] | None,
    linked: bool,
    chrome: dict,
    question: str = "",
    on_voted: Callable[[tuple[int, ...]], None] | None = None,
) -> tuple[int, ...] | None:
    """`[V]ote`: the answers with the caller's current vote ticked, and
    `[V]ote` to store the ticks. With one choice, ticking an answer
    unticks the other. Returns the vote stored, or `None` on `[B]ack`."""
    draft: Draft = {f"pick{i}": bool(mine and i in mine) for i in range(len(poll.options))}
    single = poll.max_choices == 1

    def _toggle(index: int) -> Callable[[Session, DatabaseLane, Draft], Awaitable[None]]:
        async def prompt(_session: Session, _lane: DatabaseLane, d: Draft) -> None:
            key = f"pick{index}"
            if single:
                for other in range(len(poll.options)):
                    d[f"pick{other}"] = False
                d[key] = True
            else:
                d[key] = not d[key]

        return prompt

    mark = "(*)" if single else "[x]"
    blank = "( )" if single else "[ ]"
    fields = [
        FieldSpec(
            f"pick{index}", f"Answer {index + 1}",
            lambda d, index=index, option=option: f"{mark if d[f'pick{index}'] else blank} {option}",
            _toggle(index),
            help=(
                "Enter picks this answer and clears the other." if single
                else "Enter ticks this answer, or unticks it."
            ) + " Nothing is stored until [V]ote.",
        )
        for index, option in enumerate(poll.options)
    ]

    async def _save(saved: Draft) -> tuple[int, ...]:
        picked = [index for index in range(len(poll.options)) if saved[f"pick{index}"]]
        vote = cast_vote(db, poll, board, user, picked)
        if on_voted is not None:
            on_voted(vote)
        return vote

    preamble = f"{choices_phrase(poll.max_choices)}, then [V]ote. You can change your vote until the poll closes."
    if linked and mine is None:
        preamble += f" {LINKED_VOTE_NOTICE}"
    lane = DatabaseLane(db.path)
    try:
        return await edit_resource_draft(
            session, lane, title="Vote", subtitle=sanitize_text(question) or None, fields=fields, draft=draft, save=_save, error_type=PollError,
            save_menu_text=menu_key("V", "ote"), save_hotkey="v", back_menu_text=menu_key("B", "ack"), own=True,
            save_brief="Store your vote",
            preamble=(
                colored(preamble, fg_color=MUTED_COLOR)
                if session.terminal_height >= _PREAMBLE_MIN_HEIGHT or (linked and mine is None) else None
            ),
            **chrome,
        )
    finally:
        lane.close()


def vote_summary(poll: Poll, vote: tuple[int, ...]) -> str:
    """"Voted: Yes." -- the outcome shown above the reader after a vote."""
    picked = ", ".join(cut_to_width(sanitize_text(poll.options[i]), 40) for i in vote)
    return f"Voted: {picked}."


__all__ = [
    "LINKED_VOTE_NOTICE",
    "PollDraftResult",
    "PollView",
    "WIDE_POLL_WIDTH",
    "answer_rows",
    "choices_phrase",
    "edit_poll_draft",
    "poll_block_rows",
    "result_rows",
    "review_rows",
    "vote_on_poll",
    "vote_summary",
]

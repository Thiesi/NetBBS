"""
The optional signup question (issue #835, F072).

A SysOp who approves accounts by hand had nothing to judge a signup by but
its username. On an approval-required node the SysOp may set one question
("How did you find us?", "What do you write with?"), which self-registration
asks after the password. The answer sits on the pending account for the
approver to read and is deleted once the account is approved. It was asked for
one decision, and keeping it afterwards would turn a greeting into a
profile nobody agreed to. Declining deletes it with the account.

The question is stored next to the answer: a SysOp who rewords the question
later must still see what this caller was actually asked.
"""

from __future__ import annotations

from dataclasses import dataclass

from netbbs.config import get_config, set_config
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

REGISTRATION_QUESTION_CONFIG_KEY = "registration_question"
MAX_REGISTRATION_QUESTION_LENGTH = 200
MAX_SIGNUP_ANSWER_LENGTH = 300


@dataclass(frozen=True)
class SignupAnswer:
    question: str
    answer: str
    answered_at: str


def get_registration_question(db: Database) -> str | None:
    """The question signup asks, or `None` when the SysOp set none."""
    value = (get_config(db, REGISTRATION_QUESTION_CONFIG_KEY) or "").strip()
    return value or None


def set_registration_question(db: Database, question: str | None) -> None:
    """Set the question, or clear it with `None` or blank text. Longer text
    is refused rather than cut: the SysOp is typing it and can shorten it."""
    text = (question or "").strip()
    if len(text) > MAX_REGISTRATION_QUESTION_LENGTH:
        raise ValueError(f"the question can be at most {MAX_REGISTRATION_QUESTION_LENGTH} characters")
    set_config(db, REGISTRATION_QUESTION_CONFIG_KEY, text)


def save_signup_answer(db: Database, user_id: int, *, question: str, answer: str) -> None:
    """Keep a caller's answer on their pending account. A blank answer is not
    stored, because the question is optional. A long one is cut to
    `MAX_SIGNUP_ANSWER_LENGTH`: the caller has no editor to shorten it in."""
    text = answer.strip()[:MAX_SIGNUP_ANSWER_LENGTH]
    if not text:
        return
    db.connection.execute(
        """
        INSERT INTO signup_answers (user_id, question, answer, answered_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            question = excluded.question, answer = excluded.answer, answered_at = excluded.answered_at
        """,
        (user_id, question, text, utc_now_iso()),
    )
    db.connection.commit()


def load_signup_answer(db: Database, user_id: int) -> SignupAnswer | None:
    row = db.connection.execute(
        "SELECT question, answer, answered_at FROM signup_answers WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return None
    return SignupAnswer(question=row["question"], answer=row["answer"], answered_at=row["answered_at"])

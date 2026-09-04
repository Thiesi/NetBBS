"""Shared single-key yes/no confirmations with truthful Enter defaults."""

from __future__ import annotations

from netbbs.net.char_input import EditorKeyKind
from netbbs.net.session import Session, write_prompt
from netbbs.rendering import LABEL_COLOR, MENU_KEY_COLOR, colored


def _highlighted(letter: str) -> str:
    """Highlights the letter Enter would actually choose, the same
    `MENU_KEY_COLOR`-bold treatment `netbbs.rendering.menu.menu_key`
    already uses for every other on-screen hotkey -- dogfood request.
    Deliberately not a red/green good-or-bad mapping: `prompt_yes_no` is
    reused for both safe and destructive confirmations (`Approve this
    account?` and `Delete these stale drafts now?` alike), so "yes" is
    not reliably the "good" choice to color green, nor "no" reliably
    the "bad" one to color red -- doing so would misdirect exactly the
    destructive-action prompts where getting this right matters most.
    Highlighting *the default* stays true regardless of which letter it
    is, and matches the existing capitalization convention (design doc
    §3.2's fixed-everywhere semantic-color rule) rather than
    contradicting it."""
    return colored(letter, fg_color=MENU_KEY_COLOR, bold=True)


# Dogfood follow-up: a prior dogfood request colored these brackets the
# same MENU_KEY_COLOR-bold as the highlighted default letter they hold,
# so the whole pair would "read as a keystroke zone at a glance." In
# practice that made the bracket and the letter blend into one solid-
# color block, defeating the thing that actually matters here: telling
# *the default* apart from its surrounding punctuation. The brackets
# still get their own bold emphasis -- that part of the original ask
# was right -- just in `LABEL_COLOR`, not `MENU_KEY_COLOR`: the same
# color already reserved for framing/labeling text around a value
# (`netbbs.net.mail_flow`'s "From:"/"Date:", `netbbs.net.resource_
# editor`'s own field labels) rather than the color reserved for "this
# is the actual keystroke" everywhere else in the app.
_BRACKET_OPEN = colored("[", fg_color=LABEL_COLOR, bold=True)
_BRACKET_CLOSE = colored("]", fg_color=LABEL_COLOR, bold=True)


async def read_confirmation_choice(session: Session) -> bool | None:
    """Read one valid confirmation choice.

    Returns ``True`` for Y, ``False`` for N, and ``None`` for Enter.  The
    structured editor-key stream is deliberately reused here because it is
    the narrow transport-neutral input primitive which preserves Enter;
    generic menu ``read_key()`` must continue discarding CR/LF.  Accepted
    letters are echoed and every accepted choice ends the input row.  Any
    other key rings the terminal bell and leaves the prompt active.
    """
    read_editor_key = getattr(session, "read_editor_key", None)
    while True:
        if read_editor_key is None:
            # Compatibility for lightweight Session adapters which predate
            # structured-key input (including a number of narrow test
            # doubles). Every shipped interactive transport implements
            # read_editor_key and therefore never takes this line-oriented
            # fallback; the real Telnet/SSH/web behavior is verified at the
            # wire boundary. Keeping the fallback prevents this UI helper
            # from making otherwise-valid non-interactive adapters unusable.
            while True:
                answer = (await session.read_line()).strip().lower()
                if answer == "":
                    return None
                if answer in ("y", "n"):
                    return answer == "y"
                await session.write("\a")
        try:
            key = await read_editor_key()
        except NotImplementedError:
            read_editor_key = None
            continue
        if key.kind is EditorKeyKind.ENTER:
            await session.write("\r\n")
            return None
        if key.kind is EditorKeyKind.CHAR and key.char is not None:
            answer = key.char.lower()
            if answer in ("y", "n"):
                discard_buffered_enter = getattr(session, "discard_buffered_enter", None)
                if discard_buffered_enter is not None:
                    await discard_buffered_enter()
                await session.write(f"{key.char}\r\n")
                return answer == "y"
        await session.write("\a")


async def prompt_yes_no(session: Session, prompt: str, *, default: bool) -> bool:
    """
    Ask `prompt`, appending the conventional `[Y/n]`/`[y/N]` hint
    (capitalized letter marks the default, matching every existing
    prompt's own convention -- computed here so the hint can never
    drift out of sync with the actual `default` a caller passes).

    Y/N returns immediately on that single keypress. A bare Enter selects
    ``default``. Invalid keys are rejected and the prompt remains active;
    they can never silently choose the default.
    """
    hint = f"{_highlighted('Y')}/n" if default else f"y/{_highlighted('N')}"
    await write_prompt(session, f"{prompt} {_BRACKET_OPEN}{hint}{_BRACKET_CLOSE}: ")
    answer = await read_confirmation_choice(session)
    return default if answer is None else answer


async def prompt_yes_no_or_keep(session: Session, prompt: str, *, current: bool) -> bool:
    """
    The *edit*-screen counterpart to `prompt_yes_no`: the hint shows
    only the current value (`[y]` or `[N]`, never both), and a bare
    Enter keeps it unchanged rather than selecting a fixed default --
    the same "blank = keep" convention `_prompt_optional_int`/
    `_prompt_min_age`/`_prompt_name_requirement` already use for
    non-boolean fields on the very same edit screens. Y/N returns on one
    keypress; Enter keeps the current value.
    """
    hint = _highlighted("y" if current else "N")
    await write_prompt(session, f"{prompt} {_BRACKET_OPEN}{hint}{_BRACKET_CLOSE}: ")
    answer = await read_confirmation_choice(session)
    return current if answer is None else answer

"""
`prompt_yes_no` — a yes/no confirmation that actually honors the shown
default when the user just presses Enter.

Every `[y/N]`/`[Y/n]`-style prompt in the codebase used to read via
`session.read_key()`, which by design discards CR/LF entirely (see
`netbbs.net.char_input.read_key`'s own docstring: it's built for
genuine single-letter menus, where Enter has no meaning to discard in
the first place). That made every one of those prompts' displayed
default unreachable -- pressing Enter did nothing, silently waiting for
an actual `y`/`n` keystroke, no matter what the prompt claimed. Found
across 38 call sites in four files; fixed once, here, rather than
per-site, since it was the same bug repeated rather than 38 independent
ones.

`read_line()` is what makes a real default possible: unlike
`read_key()`, it returns an empty string for a bare Enter rather than
swallowing it.
"""

from __future__ import annotations

from netbbs.net.session import Session


async def prompt_yes_no(session: Session, prompt: str, *, default: bool) -> bool:
    """
    Ask `prompt`, appending the conventional `[Y/n]`/`[y/N]` hint
    (capitalized letter marks the default, matching every existing
    prompt's own convention -- computed here so the hint can never
    drift out of sync with the actual `default` a caller passes).

    A bare Enter (`read_line()`'s own empty-string result), or anything
    that isn't recognizably `y`/`yes`/`n`/`no`, returns `default` --
    lenient on purpose, so a mistyped answer doesn't strand the user on
    a prompt that looks like a menu but requires an exact keyword.
    """
    hint = "Y/n" if default else "y/N"
    await session.write(f"{prompt} [{hint}]: ")
    answer = (await session.read_line()).strip().lower()
    if answer in ("y", "yes"):
        return True
    if answer in ("n", "no"):
        return False
    return default


async def prompt_yes_no_or_keep(session: Session, prompt: str, *, current: bool) -> bool:
    """
    The *edit*-screen counterpart to `prompt_yes_no`: the hint shows
    only the current value (`[y]` or `[N]`, never both), and a bare
    Enter keeps it unchanged rather than selecting a fixed default --
    the same "blank = keep" convention `_prompt_optional_int`/
    `_prompt_min_age`/`_prompt_name_requirement` already use for
    non-boolean fields on the very same edit screens. Same underlying
    fix as `prompt_yes_no`: `read_line()` actually returns on a bare
    Enter, unlike `read_key()`, which silently discards it.
    """
    hint = "y" if current else "N"
    await session.write(f"{prompt} [{hint}]: ")
    answer = (await session.read_line()).strip().lower()
    if answer == "y":
        return True
    if answer == "n":
        return False
    return current

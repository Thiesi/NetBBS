"""
Shared in-context help rendering (dogfood feature request, issue
#150): one small primitive reused by two genuinely different contexts
-- the fullscreen prose editor's cursor-addressed screen
(`netbbs.net.prose_editor`, Ctrl+G) and ordinary plain-scrolling
SysOp prompts (`netbbs.net.resource_editor`, Ctrl-H via
`netbbs.net.char_input.HELP_KEY`).

Deliberately does *not* clear or otherwise manage the screen around
itself -- that's a per-context concern, not something this module has
an opinion on. A cursor-addressed caller clears first and redraws its
own previous state afterward (its own existing redraw machinery
already does this for other reasons, e.g. Ctrl-L); a plain scrolling
caller just lets the help block scroll like any other inline text,
the same convention `netbbs.net.composition._show_line_editor_help`
already uses. This is what makes one function actually reusable by
both rather than two bespoke near-duplicates.
"""

from __future__ import annotations

from netbbs.net.session import Session
from netbbs.rendering import HEADER_COLOR, MUTED_COLOR, colored, visible_width, wrap_to_width


async def show_help(
    session: Session, title: str, lines: list[str], *,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
    unicode_style: bool = False,
) -> None:
    """Print a titled help block, then wait for any keystroke before
    returning. `lines` are written as-is (already-composed strings) --
    this function has no opinion on their content, only on presenting
    and waiting. A `line` that needs wrapping (see `unicode_style`
    below) must be plain, unstyled text -- wrapping is not ANSI-safe
    (`netbbs.rendering.reflow`'s own documented constraint), and every
    current caller's *short* label lines are the only ones that carry
    `colored()` styling, never the long ones.

    `header_color` defaults to the bare `theme.HEADER_COLOR` constant,
    same opt-in shape as `netbbs.rendering.layout.screen_title`'s own
    (issue #162) -- a caller with `db` in scope threads through a
    resolved `node_theme.effective_header_color_256(db)`.

    `unicode_style` (dogfood follow-up to issue #186's boxed card frame)
    gates that frame the same "box only under unicode_style, flat/
    unboxed lines otherwise" way `netbbs.net.admin_flow`'s health screen
    already established for `double_frame` -- off, this renders exactly
    as it did before #186: a plain title line followed by `lines` as-is,
    left to the terminal's own soft-wrap. On, each line is wrapped to
    the frame's own inner width first, so a line wider than the card
    (e.g. composition's Body field help) still fits inside it instead of
    spilling past the right border with only its first physical row
    prefixed."""
    if not unicode_style:
        await session.write_line(colored(title, fg_color=header_color, bold=True))
        for line in lines:
            await session.write_line(line)
        await session.write_line(colored("Press any key to continue...", fg_color=MUTED_COLOR))
        await session.read_key()
        return
    width = min(getattr(session, "terminal_width", 80), 78)
    inner_width = max(1, width - 3)
    rule_len = max(0, width - len(title) - 6)
    hdr = colored(f"╭── {title} " + "─" * rule_len + "╮", fg_color=header_color, bold=True)
    await session.write_line(f"\r\n{hdr}")
    for line in lines:
        if visible_width(line) <= inner_width:
            await session.write_line(f"│  {line}")
        else:
            for wrapped in wrap_to_width(line, inner_width):
                await session.write_line(f"│  {wrapped}")
    footer = colored("╰" + "─" * (width - 2) + "╯", fg_color=header_color, bold=True)
    await session.write_line(footer)
    await session.write_line(colored("Press any key to continue...", fg_color=MUTED_COLOR))
    await session.read_key()

"""
Shared draft-based field editor (design doc, dogfood feature request):
one screen serves both creating a new resource and editing an existing
one -- "create" is just "edit a fresh draft of defaults, then [S]ave
inserts instead of updates." Fixes two related dogfood complaints in
one shape: editing an existing board/channel/file-area/Community no
longer walks the same linear step-by-step wizard creating one does
(every field addressable independently, in any order, skipping
whatever doesn't need changing), and there is no way to be left with a
half-created resource on cancel -- nothing is written to the database
until an explicit [S]ave; [B]ack simply discards the draft.

Generalizes `netbbs.net.login_flow`'s own profile screen
(`_render_profile`/`_edit_profile`) shape -- show every field's
current value, one hotkey per field, redraw after each edit -- into a
reusable driver (`edit_resource_draft`) parameterized by a list of
`FieldSpec` entries, instead of each resource hand-writing its own
sequential prompt chain. `netbbs.net.admin_flow` supplies each
resource kind's own field list, built from a mix of this module's
generic `text_field`/`bool_field` factories and thin adapters over its
own existing per-type prompt helpers (`_prompt_optional_int`,
`_prompt_min_age`, `_prompt_name_requirement`, `_pick_optional_community`,
`_pick_optional_category`) -- this module has no knowledge of any one
resource kind's own fields, domain functions, or error types.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from netbbs.net.char_input import CANCEL_KEY, HELP_KEY, EditorKey, EditorKeyKind, reject_unhandled_key
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.help_overlay import show_help
from netbbs.net.session import Session
from netbbs.rendering import (
    ACCENT_COLOR,
    HEADER_COLOR,
    LABEL_COLOR,
    MUTED_COLOR,
    MenuEntry,
    action_bar,
    colored,
    display_width,
    menu_grid,
    sanitize_text,
    screen_title,
    wrap_to_width,
)
from netbbs.storage.execution import DatabaseLane

# A draft is a plain, freely-mutable dict of field values -- for
# "create," seeded with the resource's own sensible defaults up front
# (identical shape to an "edit" draft seeded from an existing
# resource's current values); this module never distinguishes the two
# cases itself, only the caller's own `save` closure does (calling
# create_* vs. update_*).
Draft = dict[str, Any]

# One field's own sub-interaction: reads whatever it needs (may span
# several prompts, e.g. a picker), and mutates `draft[key]` in place.
# Leaving the draft unchanged -- an invalid entry, an explicit "keep
# current" -- is always a safe, silent outcome here: unlike this
# field's `edit_resource_draft` call site, a mistake on one field never
# discards any other field already entered into the same draft.
FieldPrompt = Callable[[Session, DatabaseLane, Draft], Awaitable[None]]


@dataclass(frozen=True)
class FieldSpec:
    """One editable field on a draft-based resource editor screen.

    `menu_text` is a pre-rendered `netbbs.rendering.menu_key(...)`
    string (e.g. `menu_key("N", "ame")`) -- built by the caller, not
    this module, the same way every other menu in this codebase
    assembles its own options list; keeps this module free of any
    opinion on hotkey/prefix choices. `render(draft)` is called fresh
    on every redraw and must be a pure, cheap read of the draft, no I/O
    -- the "current value" line shown above the menu.

    `help` (dogfood feature request, issue #150), if given, is a short
    plain-text explanation shown when the caller presses Ctrl-H --
    optional and `None` by default, since authoring it for every field
    on day one isn't required (issue's own scope note); a field with no
    `help` is simply omitted from that screen.

    `brief` (issue #160's own rollout to this screen), if given, is a
    short (~30 char) one-line description shown indented under this
    field's hotkey when the caller's menu-description preference
    (`netbbs.net.menu_description_preference`) is `"brief"` or
    `"detailed"` -- see `MenuEntry`. The screen's own title already
    names the resource kind in full ("Edit message board"/"file
    area"/"chat channel"), so `brief` text may use the short noun
    ("the board", "the area") rather than repeating the full term on
    every field and blowing the width budget. At the `"detailed"`
    level, `help` is shown instead of `brief` when a field has both --
    the existing Ctrl-H writeup doubles as the richer description for
    free, no separate text required.

    `step` (dogfood feature request, issue #160's own cursor-navigation
    follow-up), if given, is a synchronous, no-I/O `(draft, direction)
    -> None` mutator that Left/Right act on when this field is the
    currently arrow-highlighted one -- `direction` is `+1`/`-1`. Only
    meaningful for a field whose value is a cycle with a real "forward"/
    "backward" (see `choice_step`); `None` by default, and Left/Right
    are a silent no-op on a field that doesn't define one (deliberately
    including `bool_field` -- see that function's own docstring for
    why arrow-triggered instant toggling was left out on purpose).
    """

    key: str
    hotkey: str
    menu_text: str
    label: str
    render: Callable[[Draft], str]
    prompt: FieldPrompt
    help: str | None = None
    step: Callable[[Draft, int], None] | None = None
    brief: str | None = None


_SAVE_BRIEF = "Write this draft to the database"
_BACK_BRIEF = "Discard the draft, nothing saved"
_BACK_BRIEF_IMMEDIATE = "Nothing pending -- already saved"


async def edit_resource_draft(
    session: Session,
    lane: DatabaseLane,
    *,
    title: str,
    subtitle: str | None = None,
    fields: list[FieldSpec],
    draft: Draft,
    save: Callable[[Draft], Awaitable[Any]] | None = None,
    error_type: type[Exception] = Exception,
    save_menu_text: str | None = None,
    back_menu_text: str,
    save_hotkey: str = "s",
    back_hotkey: str = "b",
    description_level: str = "off",
    redraw_in_place: bool = False,
    redraw_hint: bool = False,
    preamble: str | Callable[[Draft], str] | None = None,
    unicode_style: bool = False,
    collapsed: bool = False,
    accent_color: int = ACCENT_COLOR,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
) -> Any | None:
    """
    Drives one draft-based create/edit screen: renders `title` plus
    every field's current value, offers one hotkey per field (jumps
    straight to that field's own `prompt`) plus save/back, and loops
    until the caller either saves (returns whatever `save` returns) or
    backs out (returns `None`, `draft` discarded, nothing persisted or
    changed).

    `save(draft)` is the resource's own `create_*`/`update_*` call,
    already bound via closure to whatever it needs beyond the draft
    itself (`actor`, the existing resource being updated, `lane`,
    etc.) -- this function has no opinion on *how* a draft becomes a
    persisted resource, only on gathering the draft itself. `error_type`
    is caught around that call so a domain rejection (a duplicate name,
    an invalid combination) shows a friendly message and returns to the
    field menu with the draft intact, rather than crashing the session
    or silently discarding work already entered.

    Dogfood follow-up: `[B]ack` used to discard the draft unconditionally,
    even after fields had already been changed -- a SysOp who'd been
    filling in a new board/area/channel/Community for a while could lose
    all of it with one misplaced keystroke. `[B]ack`/Ctrl-C now only
    discards outright when the draft is still exactly what it started as;
    once anything differs from the starting snapshot, they're asked to
    confirm first, the same "leaving a screen with unsaved changes always
    asks" posture the fullscreen editors' own `_confirm_quit` already
    established for a different kind of content -- and can back out of
    the confirmation itself to keep editing.

    Dogfood feature request, issue #160's own follow-up: every field is
    also reachable by moving a `>` cursor with Up/Down and activating
    the highlighted one with Space or Enter (delegating to that field's
    own `prompt`, exactly what its hotkey letter already does) -- purely
    additive, every hotkey keeps working exactly as before. Nothing is
    highlighted until the first arrow press (the screen looks identical
    to today until then); Up from that unselected state lands on the
    last field, Down on the first, and the cursor then wraps at either
    end rather than stopping. Left/Right step a highlighted field's own
    `step`, if it defines one (see `FieldSpec.step`/`choice_step`);
    silently do nothing otherwise.

    `description_level` (issue #160's own rollout to this screen) is
    the caller's already-resolved `menu_description_level` preference
    ("off"/"brief"/"detailed") -- fetched once by the caller before
    entering this screen, not by this function on every redraw of its
    own loop (a per-redraw lookup here previously perturbed async
    cancellation timing elsewhere in this rollout). Field rows render
    through `menu_grid` with each `FieldSpec.brief`/`.help` as the
    description text; a field with neither shows only its hotkey label,
    identical to `description_level="off"`. Unlike the paginated picker
    screens, this one has no fallback if the whole thing doesn't fit --
    every field's current-value line plus the (now much taller,
    descriptive) menu row must all be visible at once, or the top of
    the screen scrolls off (dogfood-reported regression). The menu row
    falls back to the compact form, regardless of preference, whenever
    the descriptive form wouldn't fit this terminal at all.

    `redraw_in_place` (dogfood feature request, `netbbs.net.
    redraw_preference`) clears the terminal on every redraw instead of
    printing a fresh block below the last one -- every arrow-key/Left-
    Right/Ctrl-H press redraws this whole screen, so without it, an
    account that's actively navigating scrolls its own history away
    fast. Off by default, same "caller resolves the preference once,
    this function just trusts it" shape as `description_level`.
    `redraw_hint`, if `True`, shows a one-time contextual note after
    this screen has already redrawn at least once in place (not on the
    very first draw, before anything has scrolled) -- meant for an
    account that has never touched the preference at all
    (`redraw_in_place_ever_set`), pointing them at where to turn it on
    now that they've actually felt the thing it fixes; the caller is
    responsible for only passing `True` when `redraw_in_place` is off
    and unset, not this function.

    `subtitle`, if given, is passed straight through to `screen_title`'s
    own `subtitle` parameter -- one line under the title, above the
    underline.

    `unicode_style` (issue #160's own breadcrumb-arrow rollout, Stage 2)
    is passed straight through to `screen_title` too -- fetched once by
    the caller via `unicode_style_enabled(...)`, same "resolve once,
    pass down" shape as `description_level`/`redraw_in_place`. `False`
    by default, matching `screen_title`'s own conservative local default
    (see that function's docstring for why) -- every existing caller
    keeps today's plain "NetBBS / Title" breadcrumb until it's updated
    to pass this explicitly.

    `preamble`, if given, is shown after the title and before the field
    list -- for read-only context a screen needs above its editable
    fields (a text preview, a diagnostic line) that isn't itself one of
    `fields` because there's nothing to prompt for. Either a plain
    pre-rendered string (already `\r\n`-joined, same convention as
    `screen_title`'s own return value), or a `render(draft)`-shaped
    callable for content that must stay live across redraws the same
    way a field's own `render` does (e.g. a text preview that changes
    once its own field is edited) -- a plain string would go stale after
    the first redraw following such an edit.

    `save=None` (the default) switches this screen to *immediate* mode
    (netbbs.net.login_flow's own profile screen, issue #160's
    cursor-nav follow-up): no `[S]ave` entry is offered, and `[B]ack`/
    Ctrl-C never show the "discard unsaved changes?" confirmation --
    there is nothing pending to discard, because every field on an
    immediate-mode screen is expected to persist itself the instant
    it's activated (see `live_choice_field`) rather than waiting for a
    Save step that doesn't exist here. Every other caller keeps passing
    a real `save`/`error_type`/`save_menu_text` exactly as before; nothing
    changes for them.
    """
    initial_draft = dict(draft)
    selected: int | None = None
    redraw_count = 0
    while True:
        await session.write_line(
            "\r\n" + screen_title(
                title,
            breadcrumb=(session.node_display_name,), subtitle=subtitle, width=session.terminal_width, clear=redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient)
        )
        preamble_text = preamble(draft) if callable(preamble) else preamble
        if preamble_text:
            await session.write_line(preamble_text)
        field_line_count = 0
        for i, f in enumerate(fields):
            value = sanitize_text(f.render(draft))
            # One colored() call, not marker/label separately -- two
            # calls would insert an SGR reset between "> " and the
            # label text, splitting what should read as one contiguous
            # highlighted run.
            marker = "> " if i == selected else "  "
            prefix = colored(
                f"{marker}{f.label}", fg_color=accent_color if i == selected else LABEL_COLOR,
                bold=(i == selected),
            )
            # Dogfood report: a long field value (a free-text
            # description, most often) used to print as one raw
            # unwrapped line regardless of terminal width. Wrapped here,
            # hanging-indented to align under where the value starts on
            # line one, rather than every field growing an unconditional
            # second "Label:\nvalue" line the way a mail body's own
            # reflow block does -- this screen is dozens of short
            # one-line fields for every one that's ever actually long,
            # and forcing every field onto two lines would double this
            # screen's height for no reason.
            label_width = display_width(f"{marker}{f.label}: ")
            available = max(1, session.terminal_width - label_width)
            value_lines = wrap_to_width(value, available) or [""]
            field_line_count += len(value_lines)
            val_color = 252 if i == selected else MUTED_COLOR
            await session.write_line(f"{prefix}: {colored(value_lines[0], fg_color=val_color)}")
            indent = " " * label_width
            for continuation in value_lines[1:]:
                await session.write_line(f"{indent}{colored(continuation, fg_color=val_color)}")
        menu_entries = [MenuEntry(label=f.menu_text, brief=f.brief, detailed=f.help) for f in fields]
        if save is not None:
            menu_entries.append(MenuEntry(label=save_menu_text, brief=_SAVE_BRIEF))
        back_brief = _BACK_BRIEF if save is not None else _BACK_BRIEF_IMMEDIATE
        menu_entries.append(MenuEntry(label=back_menu_text, brief=back_brief))
        compact_menu_line = action_bar([e.label for e in menu_entries], width=session.terminal_width)
        menu_line = compact_menu_line
        if description_level != "off":
            # `menu_grid` always renders one entry per line, even with
            # descriptions off -- unlike `action_bar`'s packed single-
            # line row, that's not a byte-for-byte-compatible
            # substitute at this level. `menu_description_level`'s real
            # default is "brief", not "off" (every caller who hasn't
            # touched the setting already has it on), and this screen
            # has no pagination to fall back on the way picker.py does
            # -- the *whole* field list plus the now much taller menu
            # row must fit, or the top of the field list scrolls off
            # (dogfood-reported regression). Falls back to the compact
            # row, regardless of preference, whenever the descriptive
            # form wouldn't fit this terminal at all -- descriptions are
            # a nice-to-have, being able to see the whole screen is the
            # point.
            descriptive_menu_line = menu_grid(
                [("", menu_entries)],
                width=session.terminal_width,
                height=session.terminal_height,
                description_level=description_level,
            )
            fixed_lines = (
                (3 if subtitle else 2)  # screen_title: title [+ subtitle] + underline
                + (preamble_text.count("\r\n") + 1 if preamble_text else 0)
                + field_line_count  # every field's own value lines, wrapped ones included
                + 1  # blank line before the menu row
                + (1 if any(f.help for f in fields) else 0)  # "(Ctrl-H for help...)" hint
                + 1  # "Choice: " prompt line
            )
            descriptive_lines = descriptive_menu_line.count("\r\n") + 1
            if fixed_lines + descriptive_lines <= session.terminal_height:
                menu_line = descriptive_menu_line
        await session.write_line(f"\r\n{menu_line}")
        if any(f.help for f in fields):
            # Only hinted when at least one field actually has help
            # authored -- otherwise Ctrl-H would be an undiscoverable
            # dead end advertised on every screen (issue #150's own
            # "does not need to cover every existing feature on day
            # one" scope extends to which screens mention it at all).
            await session.write_line(colored("(Ctrl-H for help on these fields)", fg_color=MUTED_COLOR))
        if redraw_hint and redraw_count >= 1:
            await session.write_line(
                colored(
                    "(Tip: enable in-place redraw in Your profile to stop this scrolling)", fg_color=MUTED_COLOR
                )
            )
        await session.write("Choice: ")
        redraw_count += 1
        key = await _read_navigable_key(session)

        if key.kind == EditorKeyKind.UP:
            selected = len(fields) - 1 if selected is None else (selected - 1) % len(fields)
            continue
        if key.kind == EditorKeyKind.DOWN:
            selected = 0 if selected is None else (selected + 1) % len(fields)
            continue
        if key.kind in (EditorKeyKind.LEFT, EditorKeyKind.RIGHT):
            # Deliberately silent, not a bell-and-reject: pressing
            # Left/Right while sitting on a field with nothing to step
            # (or with no field highlighted at all) isn't a mistake the
            # way an unrecognized hotkey letter is, just a no-op.
            if selected is not None and fields[selected].step is not None:
                fields[selected].step(draft, 1 if key.kind == EditorKeyKind.RIGHT else -1)
            continue
        if key.kind == EditorKeyKind.ESCAPE:
            # Dogfood feature request: Esc cancels cursor-navigation
            # (drops the `>` highlight, back to plain hotkey input on
            # this same screen) rather than leaving the screen entirely
            # -- Esc backing out of the current modal state first is the
            # more predictable convention, and `[B]ack`/Ctrl-C already
            # own "actually leave." A no-op (just the bell) when nothing
            # is highlighted -- there is no cursor-nav state to cancel.
            if selected is not None:
                selected = None
                continue
            await session.write("\a")
            continue
        if key.kind == EditorKeyKind.CTRL and key.char == "h":
            await _show_field_help(
                session, fields, selected=selected, header_color=header_color, unicode_style=unicode_style,
            )
            continue
        if key.kind == EditorKeyKind.CTRL and key.char == "c":
            # Issue #157: Ctrl-C as an incremental alias for [B]ack --
            # this screen's own "discard the draft" action. Immediate
            # mode (save=None) never confirms here: every field already
            # persisted itself on activation, so there is nothing left
            # to discard.
            await session.write_line("")
            if save is not None and draft != initial_draft:
                if not await prompt_yes_no(session, "Discard unsaved changes?", default=False):
                    continue
            return None
        if key.kind == EditorKeyKind.ENTER or (key.kind == EditorKeyKind.CHAR and key.char == " "):
            if selected is None:
                # No echo happened for this keystroke either way --
                # same "just bell" reasoning `reject_unhandled_key`
                # already documents for HELP_KEY/CANCEL_KEY, not the
                # erase-last-echoed-character behavior an ordinary
                # rejected hotkey gets.
                await session.write("\a")
                continue
            await session.write_line("")
            await fields[selected].prompt(session, lane, draft)
            continue
        if key.kind != EditorKeyKind.CHAR or key.char is None:
            # Backspace/Delete/Tab/Escape/Home/End/Page Up/Page Down --
            # nothing was echoed for these either.
            await session.write("\a")
            continue

        choice = key.char.lower()
        # The `_read_navigable_key` fallback path (a Session predating
        # `read_editor_key`) surfaces HELP_KEY/CANCEL_KEY the same way
        # `read_key()` always has -- as an ordinary character equal to
        # that sentinel byte, not as EditorKeyKind.CTRL -- so both are
        # still handled here too, matching pre-#160 behavior exactly
        # for a session that can't decode arrows at all.
        if choice == HELP_KEY:
            await _show_field_help(
                session, fields, selected=selected, header_color=header_color, unicode_style=unicode_style,
            )
            continue
        if choice == back_hotkey or choice == CANCEL_KEY:
            await session.write_line("")
            if save is not None and draft != initial_draft:
                if not await prompt_yes_no(session, "Discard unsaved changes?", default=False):
                    continue
            return None
        if save is not None and choice == save_hotkey:
            await session.write_line("")
            try:
                return await save(draft)
            except error_type as exc:
                await session.write_line(colored(f"Could not save: {exc}", fg_color=MUTED_COLOR))
                continue

        field_index = next((i for i, f in enumerate(fields) if f.hotkey.lower() == choice), None)
        if field_index is None:
            await session.write(reject_unhandled_key(choice))
            continue
        selected = field_index
        await session.write_line("")
        await fields[field_index].prompt(session, lane, draft)


async def _read_navigable_key(session: Session) -> EditorKey:
    """Best-effort structured key read for `edit_resource_draft`'s
    arrow navigation -- falls back to the plain single-keystroke
    reader, wrapped as an `EditorKeyKind.CHAR`, for lightweight
    `Session` test doubles that predate `read_editor_key` (mirrors
    `netbbs.net.confirm.read_confirmation_choice`'s own identical
    fallback for the exact same reason).

    Dogfood-reported regression: `read_editor_key()` collapses 0x08 and
    0x7F into one `BACKSPACE` kind by default (both fullscreen editors
    genuinely need 0x08 to keep deleting characters), which silently
    made this screen's own Ctrl-H dead for real terminal input -- the
    `EditorKeyKind.CTRL, char="h"` branch in `edit_resource_draft` was
    unreachable outside tests, which script that event directly and
    never exercised the real byte-decoding path. `distinguish_ctrl_h`
    is passed here because this screen's own dispatch never needs a
    real Backspace at this level (typing happens inside each field's
    own sub-prompt), the same carve-out `read_key()`'s pre-existing
    `HELP_KEY` already makes for the identical byte."""
    read_editor_key = getattr(session, "read_editor_key", None)
    if read_editor_key is not None:
        try:
            return await read_editor_key(distinguish_ctrl_h=True)
        except NotImplementedError:
            pass
    raw = await session.read_key()
    return EditorKey(EditorKeyKind.CHAR, char=raw)


async def _show_field_help(
    session: Session, fields: list[FieldSpec], *, selected: int | None = None,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
    unicode_style: bool = False,
) -> None:
    """Ctrl-H's own content (issue #150): every field with a `help`
    string authored, one after another.

    Issue #160's own cursor-navigation follow-up narrows this once a
    field is arrow-highlighted: a caller already sitting on one
    specific field almost certainly wants that field's own explanation,
    not to go hunting for it in a wall of every other field's help too
    -- `selected`, if given a valid index, shows just that field's help
    (or a short "nothing written yet" note if it has none) instead of
    the full list. `None` (nothing highlighted, or a session that can
    never reach that state -- see `_read_navigable_key`'s fallback)
    keeps the original whole-screen behavior unchanged."""
    if selected is not None:
        field = fields[selected]
        if not field.help:
            await show_help(
                session, "Field help", [f"No help is available for {field.label!r} yet."],
                header_color=header_color, unicode_style=unicode_style,
            )
            return
        await show_help(
            session, "Field help", [colored(field.label, fg_color=header_color, bold=True), f"  {field.help}"],
            header_color=header_color, unicode_style=unicode_style,
        )
        return

    documented = [f for f in fields if f.help]
    if not documented:
        await show_help(
            session, "Field help", ["No help is available for this screen yet."], header_color=header_color,
            unicode_style=unicode_style,
        )
        return
    lines: list[str] = []
    for f in documented:
        lines.append(colored(f.label, fg_color=header_color, bold=True))
        lines.append(f"  {f.help}")
        lines.append("")
    await show_help(session, "Field help", lines[:-1], header_color=header_color, unicode_style=unicode_style)


def text_field(key: str, *, required: bool = False) -> FieldPrompt:
    """A plain single-line text prompt -- blank always keeps whatever
    is currently in the draft (matching every existing edit screen's
    own "blank = keep" convention); `required` only changes what the
    *current-value line* shows when the draft's value is still blank
    (a fresh "create" draft that hasn't had this field touched yet),
    never blocks typing here -- `save`'s own validation is where a
    still-blank required field actually gets rejected, the same
    "errors surface at Save, not mid-edit" shape `edit_resource_draft`
    itself already uses for domain (`error_type`) rejections."""

    async def prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        current = draft.get(key) or ""
        shown = current if current else "(blank)" if required else "(none)"
        await session.write(f"[{shown}] (blank = keep): ")
        raw = (await session.read_line()).strip()
        if raw:
            draft[key] = raw

    return prompt


def bool_field(key: str, prompt_text: str) -> FieldPrompt:
    """A toggle field -- always offers "keep current" via a bare
    Enter (`netbbs.net.confirm.prompt_yes_no_or_keep`'s own shape),
    for both a freshly-defaulted create draft and an existing value on
    edit alike.

    Deliberately has no `choice_step`-style counterpart for
    `FieldSpec.step` (issue #160's cursor-navigation follow-up): unlike
    `choice_field`, this always opens a confirming sub-prompt rather
    than toggling silently on one keystroke. Wiring Left/Right to flip
    it instantly would make the same field behave inconsistently
    depending on which key reached it -- Space/Enter/the hotkey letter
    asking first, arrows not. Left/Right are simply a no-op on a
    `bool_field` for now; making it an instant, confirmation-free
    toggle under arrow navigation would be a real, separate decision
    a boolean field's own author should make on purpose, not a side
    effect of adding `step` support in general."""
    from netbbs.net.confirm import prompt_yes_no_or_keep

    async def prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        draft[key] = await prompt_yes_no_or_keep(session, prompt_text, current=bool(draft.get(key)))

    return prompt


def choice_field(key: str, values: list[Any]) -> FieldPrompt:
    """A cycling multi-value toggle field (dogfood feature request,
    issue #153) -- `bool_field`'s "press the hotkey, no typing" shape
    generalized past two states: each press of the field's own hotkey
    advances `draft[key]` to the next entry in `values`, wrapping back
    to the first after the last. No sub-prompt, no I/O beyond the
    immediate advance -- exactly one keystroke changes the value, the
    same way `edit_resource_draft`'s outer loop already redraws the
    field's current value (via `render`) after every field
    interaction, so the caller sees each step of the cycle in turn.

    `values[0]` is the fallback starting point both when the draft
    doesn't yet contain `key` at all and when it holds a value that
    isn't one of `values` (defensive only -- every field-list caller
    seeds `key` from a real current/default value, this never happens
    in practice)."""

    async def prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        _advance_choice(key, values, draft, 1)

    return prompt


def live_choice_field(
    key: str, values: list[Any], *, persist: Callable[[DatabaseLane, Any], Awaitable[None]]
) -> FieldPrompt:
    """`choice_field`'s counterpart for an immediate-mode screen
    (`edit_resource_draft` called with `save=None`, netbbs.net.
    login_flow's own profile screen) -- there is no later Save point
    where a deferred draft value would otherwise get written, so each
    press both advances `draft[key]` to the next entry in `values` (same
    wrapping-cycle shape as `choice_field`) AND immediately persists it
    via `persist(lane, draft[key])`.

    Deliberately has no `FieldSpec.step` counterpart, unlike
    `choice_field`/`choice_step` -- `step` stays synchronous and no-I/O
    for every field across this module (see `bool_field`'s own docstring
    for the same reasoning applied to instant toggling); a live field's
    value only ever changes on Space/Enter/its hotkey, exactly like
    `choice_field` without `choice_step`."""

    async def prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        _advance_choice(key, values, draft, 1)
        await persist(lane, draft[key])

    return prompt


def _advance_choice(key: str, values: list[Any], draft: Draft, direction: int) -> None:
    current = draft.get(key, values[0])
    try:
        index = values.index(current)
    except ValueError:
        index = -1
    draft[key] = values[(index + direction) % len(values)]


def choice_step(key: str, values: list[Any]) -> Callable[[Draft, int], None]:
    """`FieldSpec.step` counterpart to `choice_field` (dogfood feature
    request, issue #160's cursor-navigation follow-up): Left/Right on a
    highlighted `choice_field`-backed field step it backward/forward
    through the exact same `values` cycle its hotkey/Space/Enter
    already advances one direction through -- same index math, just
    parameterized by `direction` instead of always `+1`. A separate
    function rather than folding into `choice_field` itself so a field
    list can keep passing `prompt=choice_field(key, values)` unchanged
    and opt into arrow support additively via `step=choice_step(key,
    values)`, matching `FieldSpec.step`'s own optional, additive
    contract."""

    def step(draft: Draft, direction: int) -> None:
        _advance_choice(key, values, draft, direction)

    return step

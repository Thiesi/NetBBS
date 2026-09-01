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
    METADATA_COLOR,
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

# Distinguishes "no field rendered yet" from "the most recently rendered
# field's own `section` really is `None`" while walking `fields` in
# `edit_resource_draft` -- both would otherwise look identical the
# moment a screen's first field happens to leave `section` unset.
_NO_SECTION_YET = object()

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

    `section` (dogfood report -- Thiesi's own observation that the main
    menu's grouped, multi-column `menu_grid` layout and this screen's
    own flat field list read as wildly different levels of polish for
    no principled reason), if given, groups this field under a bold,
    uppercased heading shared with every other field carrying the same
    `section` string -- both in the current-value list above the menu
    row and in the hotkey menu row itself (which already routes through
    `menu_grid`, so it gains real per-section columns, not just a
    heading). `None` by default, and a screen where every field leaves
    this unset renders byte-for-byte as before: sectioning only ever
    activates once a caller actually opts in, so every existing
    `edit_resource_draft` call site (create/edit forms for boards,
    channels, file areas, Communities, doors, ...) is unaffected. Fields
    sharing a section must already be adjacent in `fields` -- this
    module groups by watching for the section value changing as it
    walks the list in order, it does not sort or reindex `fields`
    itself (cursor navigation's own `selected` index has to keep meaning
    "the Nth field in this list," unchanged by sectioning).
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
    section: str | None = None


_SAVE_BRIEF = "Write this draft to the database"
_BACK_BRIEF = "Discard the draft, nothing saved"
_BACK_BRIEF_IMMEDIATE = "Nothing pending -- already saved"


def _field_value_lines(
    fields: list[FieldSpec], draft: Draft, *, selected: FieldSpec | None, accent_color: int, terminal_width: int,
) -> list[str]:
    """Pure rendering of one field-value block -- no I/O, so the exact
    same logic can compute both a dry-run line count (deciding whether
    the *full* field list fits before committing to rendering it,
    `edit_resource_draft`'s own pagination fit-check) and the real
    lines actually written, for either the whole field list or a single
    page's worth, without the two ever disagreeing on height.

    `selected` is matched by identity (`f is selected`), not position --
    a page-scoped call and a full-list call have different valid index
    ranges for "the selected field," so identity is the only primitive
    that works correctly for both without the caller reinterpreting an
    index per call."""
    lines: list[str] = []
    sectioned = any(f.section is not None for f in fields)
    previous_section = _NO_SECTION_YET
    for f in fields:
        if sectioned and f.section != previous_section:
            # Same "uppercased, bold, METADATA_COLOR" heading `menu_grid`
            # already uses for its own section titles, so a field
            # grouped this way reads as one continuous section rather
            # than two independently-styled halves of the same group.
            # No blank line before it (Codex review, PR #230): on a
            # real dense, sectioned screen at 80x24, that line alone was
            # enough to push the value list past the terminal's own
            # height on its own, before any menu content was even
            # considered. The bold uppercase heading is still a strong
            # enough visual break on its own without it.
            if f.section is not None:
                lines.append(colored(f.section.upper(), fg_color=METADATA_COLOR, bold=True))
            previous_section = f.section
        value = sanitize_text(f.render(draft))
        is_selected = f is selected
        # One colored() call, not marker/label separately -- two calls
        # would insert an SGR reset between "> " and the label text,
        # splitting what should read as one contiguous highlighted run.
        marker = "> " if is_selected else "  "
        prefix = colored(
            f"{marker}{f.label}", fg_color=accent_color if is_selected else LABEL_COLOR, bold=is_selected,
        )
        # Dogfood report: a long field value (a free-text description,
        # most often) used to print as one raw unwrapped line regardless
        # of terminal width. Wrapped here, hanging-indented to align
        # under where the value starts on line one, rather than every
        # field growing an unconditional second "Label:\nvalue" line the
        # way a mail body's own reflow block does -- this screen is
        # dozens of short one-line fields for every one that's ever
        # actually long, and forcing every field onto two lines would
        # double this screen's height for no reason.
        label_width = display_width(f"{marker}{f.label}: ")
        available = max(1, terminal_width - label_width)
        value_lines = wrap_to_width(value, available) or [""]
        val_color = 252 if is_selected else MUTED_COLOR
        lines.append(f"{prefix}: {colored(value_lines[0], fg_color=val_color)}")
        indent = " " * label_width
        for continuation in value_lines[1:]:
            lines.append(f"{indent}{colored(continuation, fg_color=val_color)}")
    return lines


def _build_menu_line(
    fields: list[FieldSpec],
    *,
    save: Callable[[Draft], Awaitable[Any]] | None,
    save_menu_text: str | None,
    back_menu_text: str,
    back_brief: str,
    description_level: str,
    session: Session,
    fixed_lines: int,
) -> str:
    """Builds the hotkey/menu row for one field subset (the whole
    screen, or one page's worth once `edit_resource_draft` has
    paginated) -- the same three-tier descriptive/sectioned-compact/
    flat fallback this screen has always used, extracted into its own
    function so the fit-check that decides whether to paginate at all
    and the real render can never disagree about what the menu row
    actually looks like.

    `fixed_lines` is everything else already committed to on screen
    (title/preamble/field values/Ctrl-H hint/prompt line, plus the
    page-position hint once paginated) -- this function only ever
    budgets its own height against what's left, it never recomputes
    anyone else's."""
    menu_entries = [MenuEntry(label=f.menu_text, brief=f.brief, detailed=f.help) for f in fields]
    if save is not None:
        menu_entries.append(MenuEntry(label=save_menu_text, brief=_SAVE_BRIEF))
    menu_entries.append(MenuEntry(label=back_menu_text, brief=back_brief))
    # Grouped into the same sections the value list above just used
    # (empty title = "no heading," `menu_grid`'s own existing
    # convention) -- a sectioned screen's menu row gets real per-section
    # columns from `menu_grid` for free, not just a heading; an
    # unsectioned screen collapses back to today's single flat group,
    # identical entries and order. [S]ave/[B]ack ride along at the end
    # of the *last* group -- the same "trails the content it acts on"
    # position every other menu row in this codebase already puts its
    # own exit/commit actions in, not a section of their own.
    menu_sections: list[tuple[str, list[MenuEntry]]] = []
    current_title: object = _NO_SECTION_YET
    for f, entry in zip(fields, menu_entries):
        section_title = f.section if f.section is not None else ""  # menu_grid uppercases its own titles
        if section_title != current_title:
            menu_sections.append((section_title, []))
            current_title = section_title
        menu_sections[-1][1].append(entry)
    if not menu_sections:
        menu_sections.append(("", []))
    menu_sections[-1][1].extend(menu_entries[len(fields):])
    # Dogfood report: on an ordinary terminal, this screen's own
    # sectioned value list above already leaves no height budget for
    # the *descriptive* menu_grid form below to fit (see that form's
    # own height-fit check) -- meaning a sectioned screen's menu row
    # fell all the way back to this compact one, which had no grouping
    # concept at all: exactly the "chaotic options list" complaint that
    # prompted sectioning in the first place, just moved from the value
    # list down to here. Built from the same `menu_sections` the
    # descriptive form uses instead of one flat `action_bar` call
    # whenever there's more than one real group -- an unsectioned
    # screen (`len(menu_sections) == 1`) renders byte-for-byte as
    # before.
    flat_menu_line = action_bar([e.label for e in menu_entries], width=session.terminal_width)
    menu_line = flat_menu_line
    if len(menu_sections) > 1:
        compact_lines: list[str] = []
        for section_title, entries in menu_sections:
            if section_title:
                compact_lines.append(colored(section_title.upper(), fg_color=METADATA_COLOR, bold=True))
            compact_lines.append(action_bar([e.label for e in entries], width=session.terminal_width))
        sectioned_compact_menu_line = "\r\n".join(compact_lines)
        # Codex review (PR #229): unlike the old always-one-line flat
        # form, a sectioned compact row grows with the section count --
        # easily enough on its own to push a real 24-row terminal's
        # field list off the top before `Choice:` ever appears, the
        # exact scroll-off regression the descriptive-form check below
        # already guards against. Reuses that same budget rather than a
        # separate one -- if even the sectioned compact row doesn't
        # fit, fall all the way back to the flat one line. (If *that*
        # doesn't fit either, `edit_resource_draft`'s own caller-side
        # fit-check is what catches it and paginates instead -- this
        # function itself has no further fallback below flat.)
        sectioned_compact_lines = sectioned_compact_menu_line.count("\r\n") + 1
        if fixed_lines + sectioned_compact_lines <= session.terminal_height:
            menu_line = sectioned_compact_menu_line
    if description_level != "off":
        # `menu_grid` always renders one entry per line, even with
        # descriptions off -- unlike `action_bar`'s packed single-line
        # row, that's not a byte-for-byte-compatible substitute at this
        # level. Falls back to the compact row, regardless of
        # preference, whenever the descriptive form wouldn't fit this
        # terminal at all -- descriptions are a nice-to-have, being able
        # to see the whole screen is the point.
        descriptive_menu_line = menu_grid(
            menu_sections,
            width=session.terminal_width,
            height=session.terminal_height,
            description_level=description_level,
        )
        descriptive_lines = descriptive_menu_line.count("\r\n") + 1
        if fixed_lines + descriptive_lines <= session.terminal_height:
            menu_line = descriptive_menu_line
    return menu_line


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
    identical to `description_level="off"`. The menu row falls back to
    the compact form, regardless of preference, whenever the
    descriptive form wouldn't fit this terminal at all.

    Codex-review-prompted (a dense, sectioned screen genuinely doesn't
    fit a real 24-row terminal no matter how the menu row degrades):
    when the *full* field list, at whichever menu-row tier it lands on,
    doesn't fit `session.terminal_height`, a *sectioned* screen
    (`FieldSpec.section` set) paginates -- one section per page, `Page
    Up`/`Page Down` (already fully decoded by `read_editor_key`, and
    previously dead-ending here at the plain-key bell-reject) cycling
    between them and wrapping at either end, same convention as
    cursor-nav Up/Down. Every hotkey keeps working regardless of which
    page is currently shown -- typing a field's own letter always jumps
    straight to it (and switches to its page so the caller sees what
    they just changed), the same "every hotkey keeps working exactly as
    before" guarantee cursor-nav itself already established. `[S]ave`/
    `[B]ack` stay reachable from every page, not gated behind reaching a
    particular one. An *unsectioned* screen has no natural page
    boundary and keeps exactly today's behavior: if it doesn't fit, the
    top of the screen scrolls off, same as always.

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
    # Computed once -- doesn't depend on `draft`. Order-preserving dedup
    # (`dict.fromkeys`) rather than `set()`: page order must match the
    # order sections first appear in `fields`, the same order the value
    # list and menu row already group by.
    section_names: list[str] = list(dict.fromkeys(f.section for f in fields if f.section is not None))
    current_page: str | None = section_names[0] if section_names else None
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

        # Codex review (PR #236): pagination specifically (not the
        # value-list/menu-row *headings*, which still use `any()` inside
        # `_field_value_lines`/`_build_menu_line` and render correctly
        # for a partially-sectioned screen) requires *every* field to
        # carry a `section` -- a field left unsectioned has no page it
        # could ever belong to (`page_fields` filters by exact section-
        # name match, and `None` was never added to `section_names`),
        # so a mixed screen jumping to that field's own hotkey would set
        # `current_page = None` and crash the next redraw at
        # `section_names.index(None)`. No real caller mixes the two
        # today (confirmed: Board/Area/Channel/Profile all section every
        # field), so this changes nothing for any screen that exists --
        # it only prevents a hypothetical future mixed screen from
        # crashing, falling back to today's un-paginated "may scroll"
        # behavior instead, the same as a screen with no sections at all.
        fully_sectioned = bool(fields) and all(f.section is not None for f in fields)
        selected_field = fields[selected] if selected is not None else None
        back_brief = _BACK_BRIEF if save is not None else _BACK_BRIEF_IMMEDIATE
        # Everything on screen except the field values and the menu row
        # itself -- both vary depending on whether this redraw ends up
        # paginated, everything here doesn't. The Ctrl-H hint is gated
        # on the *full* `fields` list even once paginated (not the
        # current page's own fields): Ctrl-H's own help screen always
        # covers every field regardless of page (see `_show_field_help`
        # below), so gating the hint on a per-page subset would make it
        # flicker on/off across pages for no reason a caller could
        # predict, and Ctrl-H itself would still work fine even on a
        # page whose hint is hidden.
        base_fixed_lines = (
            (3 if subtitle else 2)  # screen_title: title [+ subtitle] + underline
            + (preamble_text.count("\r\n") + 1 if preamble_text else 0)
            + 1  # blank line before the menu row
            + (1 if any(f.help for f in fields) else 0)  # "(Ctrl-H for help...)" hint
            + 1  # "Choice: " prompt line
        )

        # The "full" candidate is computed unconditionally every redraw
        # -- not cached -- so a mid-session terminal resize (NAWS
        # renegotiation) can un-paginate a screen that no longer needs
        # it, or paginate one that now does, the same way this screen's
        # existing menu-tier upgrades already treat terminal_width/
        # height as live values throughout. Cost is negligible (at most
        # a few dozen fields, pure, no I/O) and, in the common case
        # (still fits), these are exactly the values rendered below --
        # no separate/duplicate computation, no risk of the fit check
        # and the real render ever disagreeing.
        full_lines = _field_value_lines(
            fields, draft, selected=selected_field, accent_color=accent_color, terminal_width=session.terminal_width,
        )
        full_menu_line = _build_menu_line(
            fields, save=save, save_menu_text=save_menu_text, back_menu_text=back_menu_text, back_brief=back_brief,
            description_level=description_level, session=session,
            fixed_lines=base_fixed_lines + len(full_lines),
        )
        fits = (
            base_fixed_lines + len(full_lines) + (full_menu_line.count("\r\n") + 1) <= session.terminal_height
        )
        # Only a *sectioned* screen has a natural page boundary to fall
        # back to -- an unsectioned screen that doesn't fit keeps
        # exactly today's behavior (the top of the screen scrolls off);
        # see this function's own docstring for why that's an accepted,
        # unchanged limitation rather than something this also fixes.
        paginated = fully_sectioned and not fits

        if not paginated:
            value_lines = full_lines
            menu_line = full_menu_line
            page_hint: str | None = None
        else:
            page_fields = [f for f in fields if f.section == current_page]
            value_lines = _field_value_lines(
                page_fields, draft, selected=selected_field, accent_color=accent_color,
                terminal_width=session.terminal_width,
            )
            page_number = section_names.index(current_page) + 1
            page_hint = f"(Section {page_number} of {len(section_names)} -- PgUp/PgDn to switch)"
            menu_line = _build_menu_line(
                page_fields, save=save, save_menu_text=save_menu_text, back_menu_text=back_menu_text,
                back_brief=back_brief, description_level=description_level, session=session,
                fixed_lines=base_fixed_lines + len(value_lines) + 1,  # +1: page_hint's own line
            )

        for line in value_lines:
            await session.write_line(line)
        await session.write_line(f"\r\n{menu_line}")
        if any(f.help for f in fields):
            # Only hinted when at least one field actually has help
            # authored -- otherwise Ctrl-H would be an undiscoverable
            # dead end advertised on every screen (issue #150's own
            # "does not need to cover every existing feature on day
            # one" scope extends to which screens mention it at all).
            await session.write_line(colored("(Ctrl-H for help on these fields)", fg_color=MUTED_COLOR))
        if page_hint is not None:
            await session.write_line(colored(page_hint, fg_color=MUTED_COLOR))
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
            if paginated:
                page_indices = [i for i, f in enumerate(fields) if f.section == current_page]
                pos = page_indices.index(selected) if selected in page_indices else len(page_indices)
                selected = page_indices[(pos - 1) % len(page_indices)]
            else:
                selected = len(fields) - 1 if selected is None else (selected - 1) % len(fields)
            continue
        if key.kind == EditorKeyKind.DOWN:
            if paginated:
                page_indices = [i for i, f in enumerate(fields) if f.section == current_page]
                pos = page_indices.index(selected) if selected in page_indices else -1
                selected = page_indices[(pos + 1) % len(page_indices)]
            else:
                selected = 0 if selected is None else (selected + 1) % len(fields)
            continue
        if paginated and key.kind in (EditorKeyKind.PAGE_UP, EditorKeyKind.PAGE_DOWN):
            page_pos = section_names.index(current_page)
            step = 1 if key.kind == EditorKeyKind.PAGE_DOWN else -1
            current_page = section_names[(page_pos + step) % len(section_names)]
            # Same "any working-set change drops the highlight" precedent
            # netbbs.net.picker.pick_item's own paging already established
            # -- a `selected` index into the *previous* page's fields has
            # no meaningful counterpart on the new one.
            selected = None
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
            # Backspace/Delete/Tab/Escape/Home/End -- nothing was echoed
            # for these either. Page Up/Page Down reach here too, still
            # a no-op, whenever this redraw isn't paginated (the branch
            # above only intercepts them when it is) -- an unsectioned
            # or already-fitting screen has no page to switch to.
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
        if fields[field_index].section is not None and fields[field_index].section != current_page:
            # Every hotkey keeps working regardless of which page is
            # currently shown (cursor-nav's own established "purely
            # additive, nothing existing stops working" precedent) --
            # jump to the field's own page too, or the caller would type
            # a real hotkey, watch a field they can't see get edited, and
            # see no visible change on the next redraw.
            #
            # Codex review (PR #236): deliberately *not* gated on this
            # redraw's own `paginated` value -- the screen might fit
            # right now (nothing to jump to a page *for* yet) but stop
            # fitting by the time this field's own prompt returns (a
            # live terminal resize mid-interaction is the real case:
            # NAWS renegotiates while the caller is still typing into
            # the sub-prompt this hotkey just opened). Priming
            # `current_page` unconditionally means that if pagination
            # *does* newly activate on the very next redraw, it already
            # shows the field just edited instead of the stale default
            # (`section_names[0]`) -- harmless when it stays unpaginated,
            # since `current_page` is never consulted in that branch.
            current_page = fields[field_index].section
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

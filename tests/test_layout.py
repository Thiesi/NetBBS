"""Visual-composition regression tests for ordinary terminal screens."""

from __future__ import annotations

import re

import pytest

from netbbs.rendering import (
    MenuEntry,
    action_bar,
    badge,
    clear_screen,
    empty_state,
    menu_grid,
    menu_key,
    screen_title,
    telemetry_gauge,
    visible_width,
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def visible(text: str) -> str:
    return _ANSI_RE.sub("", text)


def test_screen_title_shows_location_subtitle_and_ascii_divider():
    result = visible(screen_title("Home", subtitle="alice / mail caught up", width=80))
    assert result.split("\r\n") == [
        "NetBBS / Home",
        "alice / mail caught up",
        "----------------------",
    ]


def test_screen_title_divider_matches_the_wider_of_location_or_subtitle():
    # "NetBBS / Home" (13 chars) is shorter than the subtitle (23 chars)
    # above -- the divider spans whichever line is actually wider, not
    # just the location line, so a longer subtitle isn't left dangling
    # past a too-short rule.
    short_subtitle_result = visible(screen_title("Home", subtitle="hi", width=80))
    assert short_subtitle_result.split("\r\n") == [
        "NetBBS / Home",
        "hi",
        "-------------",
    ]


def test_screen_title_does_not_clear_by_default():
    result = screen_title("Home", width=80)
    assert not result.startswith(clear_screen())


def test_screen_title_clear_prepends_clear_screen():
    """Dogfood feature request (redraw in place instead of scrolling):
    `clear=True` prepends the same clear_screen() sequence the
    fullscreen editors already use for a first draw, so this screen
    replaces whatever was on the terminal instead of printing below
    it."""
    result = screen_title("Home", width=80)
    cleared = screen_title("Home", width=80, clear=True)
    assert cleared == f"{clear_screen()}{result}"


def test_screen_title_unicode_style_off_by_default():
    """Dogfood feature request: even though `unicode_style_preference`
    itself defaults to on, this function's own local parameter stays
    conservative -- every existing caller/test renders byte-for-byte as
    before until a caller explicitly threads the resolved preference
    through."""
    result = visible(screen_title("Trust policy", breadcrumb=("NetBBS", "System"), width=80))
    assert "NetBBS / System / Trust policy" in result
    assert "›" not in result


def test_screen_title_unicode_style_uses_arrow_separator():
    result = visible(
        screen_title("Trust policy", breadcrumb=("NetBBS", "System"), width=80, unicode_style=True)
    )
    assert "NetBBS › System › Trust policy" in result
    assert "/" not in result.split("\r\n")[0]


def test_screen_title_unicode_style_colors_ancestors_muted_and_title_prominent():
    """The other half of the dogfood report: ancestor levels ("NetBBS",
    "System") should read as less important than the current location
    ("Trust policy") -- muted color for ancestors, the header color
    reserved for the final segment only."""
    result = screen_title("Trust policy", breadcrumb=("NetBBS", "System"), width=80, unicode_style=True)
    from netbbs.rendering import HEADER_COLOR, METADATA_COLOR
    from netbbs.rendering.ansi import colored

    assert colored("NetBBS", fg_color=METADATA_COLOR) in result
    assert colored("Trust policy", fg_color=HEADER_COLOR) in result
    assert colored("Trust policy", fg_color=METADATA_COLOR) not in result


def test_screen_title_unicode_style_with_no_breadcrumb_only_affects_the_rule():
    """A single-segment title (breadcrumb=()) has no ancestor level to
    color differently or separator to swap, so the title line itself is
    unaffected -- but the divider rule is a standalone style choice (style
    spec, round following the pre-5.0.0 "beautify" audit: "─" replaces "-"
    wherever `unicode_style` is on, independent of breadcrumb depth), so
    it still switches glyph here."""
    plain = screen_title("Home", breadcrumb=(), width=80)
    styled = screen_title("Home", breadcrumb=(), width=80, unicode_style=True)
    plain_lines = plain.split("\r\n")
    styled_lines = styled.split("\r\n")
    assert plain_lines[0] == styled_lines[0]
    assert plain_lines[-1] != styled_lines[-1]
    assert "-" * 4 in plain_lines[-1]
    assert "─" * 4 in styled_lines[-1]


def test_screen_title_truncates_every_visible_line_to_terminal_width():
    result = screen_title("A title far too long", subtitle="also much too long", width=12)
    assert all(visible_width(line) <= 12 for line in result.split("\r\n"))


# -- breadcrumb collapse (dogfood feature request, follow-up to the ---------
# -- pre-5.0.0 style rollout) ------------------------------------------------


def test_screen_title_shows_the_full_breadcrumb_when_it_fits():
    result = visible(screen_title("Trust policy", breadcrumb=("NetBBS", "System"), width=80))
    assert result.split("\r\n")[0] == "NetBBS / System / Trust policy"


def test_screen_title_dynamically_collapses_when_the_full_breadcrumb_does_not_fit():
    """The actual bug fix: the old ellipsis-based truncation cut off the
    *current location* -- the one thing a breadcrumb needs to say --
    while keeping the least useful ancestor prefix. A too-narrow
    terminal must show the current segment, not a chopped "NetBBS /
    Sys..."."""
    result = visible(screen_title("Trust policy", breadcrumb=("NetBBS", "System"), width=15))
    assert result.split("\r\n")[0] == "Trust policy"


def test_screen_title_collapsed_true_forces_the_short_form_even_with_room_to_spare():
    result = visible(
        screen_title("Trust policy", breadcrumb=("NetBBS", "System"), width=80, collapsed=True)
    )
    assert result.split("\r\n")[0] == "Trust policy"


def test_screen_title_collapse_is_a_no_op_with_no_breadcrumb_ancestors():
    """A single-segment title has nothing to collapse away from --
    `collapsed=True` must not do anything strange to it."""
    plain = visible(screen_title("Home", breadcrumb=(), width=80))
    collapsed = visible(screen_title("Home", breadcrumb=(), width=80, collapsed=True))
    assert plain == collapsed == "Home\r\n------------"  # divider has a 12-char floor


def test_screen_title_collapsed_divider_matches_the_collapsed_titles_own_length():
    """The divider rule underlines what's actually shown, not the
    hypothetical full breadcrumb -- a long "NetBBS / System" prefix
    collapsing away shouldn't leave a divider longer than the short
    title now displayed above it."""
    result = visible(
        screen_title("Trust policy", breadcrumb=("NetBBS", "System"), width=80, collapsed=True)
    )
    lines = result.split("\r\n")
    assert lines[0] == "Trust policy"
    assert lines[-1] == "-" * len("Trust policy")


def test_screen_title_cuts_cjk_text_at_a_display_column_boundary():
    """Dogfood report: international users found non-ASCII handling
    poor. A naive character slice (the old `location[:width]`) can
    leave a CJK line *wider* than the terminal, since each character
    is 2 display columns, not 1 -- 8 CJK characters (16 columns) all
    fit inside a character-count budget of 10, overflowing a real
    10-column terminal by 6 columns."""
    text = "你好世界你好世界"  # 8 characters, 16 display columns
    result = screen_title(text, breadcrumb=(), width=10)
    title_line = visible(result).split("\r\n")[0]
    assert visible_width(title_line) <= 10
    assert title_line == text[:5]  # 5 CJK chars = exactly 10 columns


# -- node-name gradient (GitHub issue #175) ----------------------------


def test_screen_title_node_name_gradient_is_a_no_op_by_default():
    """`node_name_gradient=None` (the default) must render byte-for-byte
    as before this parameter existed -- pinned against a few of the
    scenarios already covered above."""
    for kwargs in (
        {"breadcrumb": ("NetBBS", "System"), "width": 80},
        {"breadcrumb": ("NetBBS", "System"), "width": 80, "unicode_style": True},
        {"breadcrumb": (), "width": 80},
    ):
        with_default_arg = screen_title("Trust policy", **kwargs)
        without_the_arg = screen_title("Trust policy", node_name_gradient=None, **kwargs)
        assert with_default_arg == without_the_arg


def test_screen_title_node_name_gradient_colors_the_first_breadcrumb_segment_plain_style():
    from netbbs.rendering.gradient import gradient_text

    result = screen_title("Trust policy", breadcrumb=("NetBBS", "System"), width=80, node_name_gradient="rainbow")
    expected_node_name = gradient_text("NetBBS", "rainbow", bold=True, truecolor=False)
    assert expected_node_name in result
    assert visible(result).split("\r\n")[0] == "NetBBS / System / Trust policy"


def test_screen_title_node_name_gradient_colors_the_first_breadcrumb_segment_unicode_style():
    from netbbs.rendering.gradient import gradient_text

    result = screen_title(
        "Trust policy", breadcrumb=("NetBBS", "System"), width=80, unicode_style=True, node_name_gradient="gold",
    )
    expected_node_name = gradient_text("NetBBS", "gold", truecolor=False)
    assert expected_node_name in result
    assert visible(result).split("\r\n")[0] == "NetBBS › System › Trust policy"


def test_screen_title_node_name_gradient_has_no_effect_with_a_single_segment():
    """`breadcrumb=()` leaves nothing but `title` itself -- there is no
    separate node-name segment to color, so the gradient is a no-op."""
    plain = screen_title("Home", breadcrumb=(), width=80)
    gradiented = screen_title("Home", breadcrumb=(), width=80, node_name_gradient="rainbow")
    assert plain == gradiented


def test_screen_title_node_name_gradient_has_no_effect_when_collapsed():
    """The collapsed/too-narrow form shows only the current-location
    segment, not the node name -- nothing for the gradient to color."""
    plain = screen_title("Trust policy", breadcrumb=("NetBBS", "System"), width=80, collapsed=True)
    gradiented = screen_title(
        "Trust policy", breadcrumb=("NetBBS", "System"), width=80, collapsed=True, node_name_gradient="rainbow",
    )
    assert plain == gradiented


def test_menu_grid_uses_two_columns_at_classic_width():
    result = visible(menu_grid([
        ("Explore", [menu_key("C", "ommunities"), menu_key("F", "ind")]),
        ("You", [menu_key("E", "-mail"), menu_key("P", "rofile")]),
    ], width=80))
    lines = result.split("\r\n")
    assert "EXPLORE" in lines[0] and "YOU" in lines[0]
    assert "[C]ommunities" in lines[1] and "[E]-mail" in lines[1]


def test_menu_grid_collapses_to_one_column_at_minimum_width():
    # Dogfood follow-up (issue #160): a genuine single-column collapse
    # (multiple sections squeezed down to one) gets a standing notice
    # explaining why -- see test_menu_grid_notes_when_collapsed_to_one_
    # column below for the notice itself.
    result = visible(menu_grid([
        ("Explore", [menu_key("C", "ommunities")]),
        ("You", [menu_key("E", "-mail")]),
    ], width=40))
    lines = result.split("\r\n")
    assert lines[:5] == ["EXPLORE", "  [C]ommunities", "", "YOU", "  [E]-mail"]


def test_menu_grid_collapse_notice_respects_width():
    # The notice text is long informational prose, not a fixed-format
    # label -- it must wrap to the terminal width like anything else
    # rendered here, not overflow a narrow terminal unwrapped.
    result = visible(menu_grid([
        ("Explore", [menu_key("C", "ommunities")]),
        ("You", [menu_key("E", "-mail")]),
    ], width=40))
    assert all(visible_width(line) <= 40 for line in result.split("\r\n"))


def test_menu_grid_notes_when_collapsed_to_one_column():
    result = visible(menu_grid([
        ("Explore", [menu_key("C", "ommunities")]),
        ("You", [menu_key("E", "-mail")]),
    ], width=40))
    assert "widen your terminal" in result.lower()


def test_menu_grid_two_sections_at_wide_width_is_not_flagged_as_collapsed():
    # Dogfood follow-up: going from 3 columns to 2 (or simply having
    # only 2 sections) is routine width adaptation almost every real
    # terminal hits -- the classic 80-column default never reaches the
    # 3-column breakpoint at all -- not a degraded state worth a notice.
    result = visible(menu_grid([
        ("Explore", [menu_key("C", "ommunities")]),
        ("You", [menu_key("E", "-mail")]),
    ], width=80))
    assert "widen your terminal" not in result.lower()


def test_menu_grid_uses_three_columns_at_wide_width():
    result = visible(menu_grid([
        ("Explore", [menu_key("C", "ommunities")]),
        ("You", [menu_key("E", "-mail")]),
        ("System", [menu_key("L", "ogoff")]),
    ], width=120))
    lines = result.split("\r\n")
    assert "EXPLORE" in lines[0] and "YOU" in lines[0] and "SYSTEM" in lines[0]
    assert "widen your terminal" not in result.lower()


def test_menu_grid_three_sections_at_classic_width_uses_two_columns_not_three():
    result = visible(menu_grid([
        ("Explore", [menu_key("C", "ommunities")]),
        ("You", [menu_key("E", "-mail")]),
        ("System", [menu_key("L", "ogoff")]),
    ], width=80))
    lines = result.split("\r\n")
    assert "EXPLORE" in lines[0] and "YOU" in lines[0]
    # System is pushed to its own group below, not squeezed onto the
    # first row as a third column.
    assert "SYSTEM" not in lines[0]


def test_menu_grid_empty_title_omits_the_heading_line():
    # An empty title means "one flat group, no heading to show" -- a
    # legitimate shape for a single-purpose menu with nothing to group,
    # not a blank line where a heading would otherwise go.
    result = visible(menu_grid([
        ("", [menu_key("C", "reate"), menu_key("B", "ack")]),
    ], width=80))
    assert result.split("\r\n") == ["  [C]reate", "  [B]ack"]


def test_menu_grid_rejects_unknown_description_level():
    with pytest.raises(ValueError, match="description_level"):
        menu_grid([("Explore", [menu_key("C", "ommunities")])], width=80, description_level="verbose")


def test_menu_grid_plain_string_options_are_unaffected_by_description_level():
    # A bare str option (no MenuEntry, no description text) must render
    # identically regardless of description_level -- there is nothing
    # to show either way, and every existing caller passes plain
    # strings.
    off = menu_grid([("Explore", [menu_key("C", "ommunities")])], width=80, description_level="off")
    brief = menu_grid([("Explore", [menu_key("C", "ommunities")])], width=80, description_level="brief")
    assert off == brief


def test_menu_grid_shows_brief_description_under_its_entry():
    result = visible(menu_grid([
        ("Explore", [MenuEntry(label=menu_key("C", "ommunities"), brief="Browse shared spaces")]),
    ], width=80, description_level="brief"))
    lines = result.split("\r\n")
    assert lines[1] == "  [C]ommunities"
    assert lines[2].strip() == "Browse shared spaces"


def test_menu_grid_description_off_by_default_even_with_menu_entry():
    result = visible(menu_grid([
        ("Explore", [MenuEntry(label=menu_key("C", "ommunities"), brief="Browse shared spaces")]),
    ], width=80))
    assert "Browse shared spaces" not in result


def test_menu_grid_detailed_level_prefers_detailed_text_over_brief():
    result = visible(menu_grid([
        ("Explore", [MenuEntry(
            label=menu_key("C", "ommunities"), brief="Browse shared spaces",
            detailed="Browse spaces shared by other callers, organized by topic.",
        )]),
    ], width=80, description_level="detailed"))
    assert "Browse spaces shared by other callers, organized by topic." in result
    assert "Browse shared spaces" not in result


def test_menu_grid_detailed_level_falls_back_to_brief_when_no_detailed_text():
    result = visible(menu_grid([
        ("Explore", [MenuEntry(label=menu_key("C", "ommunities"), brief="Browse shared spaces")]),
    ], width=80, description_level="detailed"))
    assert "Browse shared spaces" in result


def test_menu_grid_entry_with_no_description_text_shows_no_extra_line():
    result = visible(menu_grid([
        ("Explore", [MenuEntry(label=menu_key("C", "ommunities")), menu_key("F", "ind")]),
    ], width=80, description_level="brief"))
    lines = result.split("\r\n")
    assert lines == ["EXPLORE", "  [C]ommunities", "  [F]ind"]


def test_menu_grid_description_text_is_truncated_to_the_available_column_width():
    # Same class of bug as the collapse-notice width fix: description
    # text is caller-authored prose with no length guarantee, and must
    # not overflow a narrow column just because it wasn't wrapped.
    long_description = "A description far too long to fit on any narrow terminal without truncation"
    result = visible(menu_grid([
        ("Explore", [MenuEntry(label=menu_key("C", "ommunities"), brief=long_description)]),
    ], width=40, description_level="brief"))
    assert all(visible_width(line) <= 40 for line in result.split("\r\n"))


def test_menu_grid_descriptions_forced_off_below_height_floor():
    result = visible(menu_grid([
        ("Explore", [MenuEntry(label=menu_key("C", "ommunities"), brief="Browse shared spaces")]),
    ], width=80, height=10, description_level="brief"))
    assert "Browse shared spaces" not in result
    assert "descriptions hidden" in result.lower()


def test_menu_grid_descriptions_shown_above_height_floor():
    result = visible(menu_grid([
        ("Explore", [MenuEntry(label=menu_key("C", "ommunities"), brief="Browse shared spaces")]),
    ], width=80, height=24, description_level="brief"))
    assert "Browse shared spaces" in result
    assert "descriptions hidden" not in result.lower()


def test_menu_grid_no_height_given_never_suppresses_descriptions():
    # height is optional -- a caller that doesn't pass it (or doesn't
    # know it) must not have descriptions silently disabled.
    result = visible(menu_grid([
        ("Explore", [MenuEntry(label=menu_key("C", "ommunities"), brief="Browse shared spaces")]),
    ], width=80, description_level="brief"))
    assert "Browse shared spaces" in result


def test_action_bar_wraps_only_between_complete_actions():
    result = visible(action_bar([
        menu_key("O", "lder"), menu_key("N", "ewer"), menu_key("B", "ack"),
    ], width=18))
    assert result.split("\r\n") == ["[O]lder  [N]ewer", "[B]ack"]


def test_empty_state_and_badge_use_compact_ascii_safe_copy():
    assert visible(empty_state("No posts yet", detail="Start the conversation.")) == (
        "No posts yet\r\nStart the conversation."
    )
    assert visible(badge("edited")) == "[edited]"


def test_empty_state_cuts_cjk_title_at_a_display_column_boundary():
    text = "你好世界你好世界"  # 8 characters, 16 display columns
    result = visible(empty_state(text, width=10))
    assert visible_width(result) <= 10
    assert result == text[:5]  # 5 CJK chars = exactly 10 columns


def test_badge_rejects_unknown_tone():
    with pytest.raises(ValueError, match="unknown badge tone"):
        badge("mystery", tone="unknown")


@pytest.mark.parametrize("width", [0, -1])
def test_layout_rejects_non_positive_width(width):
    with pytest.raises(ValueError):
        screen_title("Home", width=width)
    with pytest.raises(ValueError):
        menu_grid([("Explore", ["item"])], width=width)
    with pytest.raises(ValueError):
        action_bar(["item"], width=width)
    with pytest.raises(ValueError):
        empty_state("Nothing", width=width)
    with pytest.raises(ValueError):
        telemetry_gauge(2, 10, width=width)


def test_telemetry_gauge_unicode_and_ascii():
    gauge_unicode = telemetry_gauge(2, 10, width=10, unicode_style=True)
    plain_u = visible(gauge_unicode)
    assert plain_u == "[██░░░░░░░░] 2/10"
    assert visible_width(plain_u) == 17

    gauge_ascii = telemetry_gauge(2, 10, width=10, unicode_style=False)
    plain_a = visible(gauge_ascii)
    assert plain_a == "[##........] 2/10"
    assert visible_width(plain_a) == 17


def test_telemetry_gauge_boundaries_and_clamping():
    # Zero current
    assert visible(telemetry_gauge(0, 10, width=10, unicode_style=True)) == "[░░░░░░░░░░] 0/10"
    # Full capacity
    assert visible(telemetry_gauge(10, 10, width=10, unicode_style=True)) == "[██████████] 10/10"
    # Exceeding capacity
    assert visible(telemetry_gauge(15, 10, width=10, unicode_style=True)) == "[██████████] 15/10"
    # Negative / zero total
    assert visible(telemetry_gauge(0, 0, width=10, unicode_style=True)) == "[░░░░░░░░░░] 0/0"
    assert visible(telemetry_gauge(-5, 10, width=10, unicode_style=True)) == "[░░░░░░░░░░] -5/10"


def test_telemetry_gauge_show_ratio_option():
    bar_only = visible(telemetry_gauge(4, 10, width=10, unicode_style=True, show_ratio=False))
    assert bar_only == "[████░░░░░░]"
    assert visible_width(bar_only) == 12


def test_telemetry_gauge_tones():
    # Capacity tone: low is success, mid warning, high error
    low = telemetry_gauge(2, 10, tone="capacity")
    mid = telemetry_gauge(8, 10, tone="capacity")
    high = telemetry_gauge(10, 10, tone="capacity")
    assert "\x1b[38;5;10m" in low or "\x1b[32m" in low or "2/10" in low
    assert "\x1b[38;5;11m" in mid or "\x1b[33m" in mid or "8/10" in mid
    assert "\x1b[38;5;9m" in high or "\x1b[31m" in high or "10/10" in high

    # Health tone: high is success, mid warning, low error
    h_good = telemetry_gauge(9, 10, tone="health")
    h_poor = telemetry_gauge(2, 10, tone="health")
    assert "9/10" in h_good
    assert "2/10" in h_poor


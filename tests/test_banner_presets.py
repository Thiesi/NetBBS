from __future__ import annotations

from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Callable

import pytest

from netbbs.net.banner_presets import (
    BOARD_LIST_MASTHEAD_PRESETS,
    CHAT_CHANNEL_PICKER_MASTHEAD_PRESETS,
    FILE_AREA_MASTHEAD_PRESETS,
    LOGOFF_BANNER_PRESETS,
    MAIN_MENU_BANNER_PRESETS,
    NEW_ACCOUNT_BANNER_AFTER_PRESETS,
    NEW_ACCOUNT_BANNER_BEFORE_PRESETS,
    WELCOME_BANNER_PRESETS,
    BannerPreset,
    load_board_list_masthead_preset,
    load_chat_channel_picker_masthead_preset,
    load_file_area_masthead_preset,
    load_logoff_banner_preset,
    load_main_menu_banner_preset,
    load_new_account_banner_after_preset,
    load_new_account_banner_before_preset,
    load_welcome_banner_preset,
)
from netbbs.rendering.ansi import strip_ansi
from netbbs.rendering.ansi_art import decode_ansi_bytes
from netbbs.rendering.width import display_width


PresetLoader = Callable[[BannerPreset], bytes]

PRESET_FAMILIES: tuple[tuple[str, tuple[BannerPreset, ...], PresetLoader], ...] = (
    ("welcome", WELCOME_BANNER_PRESETS, load_welcome_banner_preset),
    ("main menu", MAIN_MENU_BANNER_PRESETS, load_main_menu_banner_preset),
    ("logoff", LOGOFF_BANNER_PRESETS, load_logoff_banner_preset),
    ("new account before", NEW_ACCOUNT_BANNER_BEFORE_PRESETS, load_new_account_banner_before_preset),
    ("new account after", NEW_ACCOUNT_BANNER_AFTER_PRESETS, load_new_account_banner_after_preset),
    ("board list", BOARD_LIST_MASTHEAD_PRESETS, load_board_list_masthead_preset),
    ("file area", FILE_AREA_MASTHEAD_PRESETS, load_file_area_masthead_preset),
    (
        "chat channel picker",
        CHAT_CHANNEL_PICKER_MASTHEAD_PRESETS,
        load_chat_channel_picker_masthead_preset,
    ),
)

RESOURCE_ROOT = Path(__file__).parents[1] / "src" / "netbbs" / "net" / "banner_presets"
RESOURCE_FAMILIES: tuple[tuple[str, tuple[BannerPreset, ...]], ...] = (
    ("welcome", WELCOME_BANNER_PRESETS),
    ("masthead", MAIN_MENU_BANNER_PRESETS),
    ("logoff", LOGOFF_BANNER_PRESETS),
    ("new_account_before", NEW_ACCOUNT_BANNER_BEFORE_PRESETS),
    ("new_account_after", NEW_ACCOUNT_BANNER_AFTER_PRESETS),
    ("board_list", BOARD_LIST_MASTHEAD_PRESETS),
    ("file_area", FILE_AREA_MASTHEAD_PRESETS),
    ("chat_channel_picker", CHAT_CHANNEL_PICKER_MASTHEAD_PRESETS),
)


@pytest.mark.parametrize(("directory", "presets"), RESOURCE_FAMILIES)
def test_registry_exactly_matches_packaged_assets(
    directory: str, presets: tuple[BannerPreset, ...]
) -> None:
    registered = {preset.resource for preset in presets}
    packaged = {path.name for path in (RESOURCE_ROOT / directory).glob("*.ans")}

    assert registered == packaged


@pytest.mark.parametrize(("family", "presets", "loader"), PRESET_FAMILIES)
def test_every_preset_is_packaged_reset_and_fits_80_columns(
    family: str, presets: tuple[BannerPreset, ...], loader: PresetLoader
) -> None:
    assert presets, family
    assert len({preset.key for preset in presets}) == len(presets)

    for preset in presets:
        text = decode_ansi_bytes(loader(preset))
        assert "\x1b[" in text, f"{family}/{preset.resource} has no ANSI styling"
        assert text.rstrip("\r\n").endswith("\x1b[0m"), f"{family}/{preset.resource} leaks terminal state"
        for line_number, line in enumerate(text.splitlines(), start=1):
            width = display_width(strip_ansi(line))
            assert width <= 80, f"{family}/{preset.resource}:{line_number} is {width} columns"


@pytest.mark.parametrize(("family", "presets", "loader"), PRESET_FAMILIES[1:])
def test_curated_family_has_no_near_duplicate_compositions(
    family: str, presets: tuple[BannerPreset, ...], loader: PresetLoader
) -> None:
    visible = {
        preset.key: strip_ansi(decode_ansi_bytes(loader(preset))).replace("\r", "").strip()
        for preset in presets
    }

    for (left_key, left), (right_key, right) in combinations(visible.items(), 2):
        similarity = SequenceMatcher(None, left, right).ratio()
        assert similarity < 0.90, (
            f"{family} presets {left_key!r} and {right_key!r} are {similarity:.1%} similar"
        )


def test_masthead_categories_ship_distinct_artwork() -> None:
    families = (
        (MAIN_MENU_BANNER_PRESETS, load_main_menu_banner_preset),
        (BOARD_LIST_MASTHEAD_PRESETS, load_board_list_masthead_preset),
        (FILE_AREA_MASTHEAD_PRESETS, load_file_area_masthead_preset),
        (CHAT_CHANNEL_PICKER_MASTHEAD_PRESETS, load_chat_channel_picker_masthead_preset),
    )
    contents = [{loader(preset) for preset in presets} for presets, loader in families]

    for left, right in combinations(contents, 2):
        assert left.isdisjoint(right)

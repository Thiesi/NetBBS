"""
Bundled banner and masthead sample presets.

Shipped as real installed package data -- the same mechanism `pyproject.
toml` already uses for `netbbs.web`'s own `static/*` -- specifically
because `examples/` (where these samples used to live) is *not* part of
the installed wheel at all: `[tool.setuptools.packages.find]` is scoped
to `src/` only. An operator running the actually-supported install path
(a release wheel, per `docs/NetBBS-operator-guide.md`) had no sample
files on their filesystem to `cp` into place in the first place -- the
gap this closes is bigger than "manual copying is annoying," it's "the
samples were unreachable from a real install."

Every `[G]allery` screen has a deliberately screen-specific collection:
welcome, main menu, logoff, both registration moments, and the three
section pickers do not masquerade the same generic strips as distinct
samples.  The gallery previews a preset through the same path as its
own `[P]review`, then writes the selected bytes to that surface's
singleton file -- zero operator filesystem access needed and identical
behavior for a wheel install or a source checkout.

The collections are curated for visibly different silhouettes, density,
and visual rhythm as well as different palettes.  Near-identical template
recolors do not belong in the library.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class BannerPreset:
    key: str
    name: str
    depth: str  # "truecolor (24-bit RGB)" or "256-color extended ANSI"
    description: str
    resource: str  # filename within the collection's package-data subdirectory


WELCOME_BANNER_PRESETS: tuple[BannerPreset, ...] = (
    BannerPreset(
        key="synthwave_magenta_cyan", name="Synthwave / Magenta-Cyan Neon Grid", depth="truecolor (24-bit RGB)",
        description=(
            "Vibrant 24-bit RGB neon magenta-to-gold-to-cyan gradients, a chromatic sunset "
            "wordmark, a stylized half-block horizon perspective grid (░▒▓█▀▄), and modern "
            "rounded box-drawing (╭─╮)."
        ),
        resource="synthwave_magenta_cyan.ans",
    ),
    BannerPreset(
        key="aurora_violet_emerald", name="Celestial Nebula / Violet-Emerald Aurora", depth="truecolor (24-bit RGB)",
        description=(
            "Deep astral violet-to-emerald aurora ribbons, starfield constellation accents "
            "(✦ ✧ ★ ⋆ ✶), double-line architectural framing (╔═╗), and a clean multi-column "
            "system telemetry layout."
        ),
        resource="aurora_violet_emerald.ans",
    ),
    BannerPreset(
        key="cyberpunk_sunset_gold", name="Cyberpunk Megacity / Sunset Amber-Gold", depth="truecolor (24-bit RGB)",
        description=(
            "High-saturation blood orange to sunset amber gradient horizon with half-block "
            "skyline silhouette (▀▄█), segmented telemetry HUD, and heavy border brackets (┏━┓)."
        ),
        resource="cyberpunk_sunset_gold.ans",
    ),
    BannerPreset(
        key="deep_ocean_sapphire_teal", name="Abyssal Ocean / Sapphire-Teal", depth="truecolor (24-bit RGB)",
        description=(
            "Sub-surface ocean mesh theme with deep navy to bioluminescent teal gradients, "
            "wave crest artwork, oceanic telemetry badges, and clean rounded frames (╭─╮)."
        ),
        resource="deep_ocean_sapphire_teal.ans",
    ),
    BannerPreset(
        key="solar_flare_crimson_amber", name="Solar Flare / Crimson-Amber Supernova", depth="truecolor (24-bit RGB)",
        description=(
            "High-intensity solar prominence arches, supernova crimson-to-gold plasma gradients, "
            "fusion core metrics, and glowing dither pulse bars (░▒▓█▓▒░)."
        ),
        resource="solar_flare_crimson_amber.ans",
    ),
    BannerPreset(
        key="matrix_phosphor_green", name="Cyber Matrix / Green Phosphor CRT", depth="256-color extended ANSI",
        description=(
            "High-contrast CRT green phosphor palette (shades 22-190), hexadecimal tech "
            "brackets (⬡ ⬢ ⎔ ⏣), segmented signal meters (▰▰▰▱▱), and fine scanline optical "
            "dithering."
        ),
        resource="matrix_phosphor_green.ans",
    ),
    BannerPreset(
        key="nord_frost_slate", name="Nordic Frost / Polar Slate & Cyan", depth="256-color extended ANSI",
        description=(
            "Ice cyan and slate blue accents (shades 31, 81, 117, 254), smooth rounded corners "
            "(╭─╮), diamond bullet hierarchies (◆ ◇ ◈), and discrete service pill badges."
        ),
        resource="nord_frost_slate.ans",
    ),
    BannerPreset(
        key="dracula_purple_pink", name="Dracula Night / Purple & Neon Pink", depth="256-color extended ANSI",
        description=(
            "Gothic modern dark mode aesthetic with Dracula purple (141), neon pink (212), "
            "vampire telemetry, bat/star accents (❖ ✦), and double-line framing (╔═╗)."
        ),
        resource="dracula_purple_pink.ans",
    ),
    BannerPreset(
        key="tokyo_night_storm", name="Tokyo Night / Shibuya Cyan-Magenta", depth="256-color extended ANSI",
        description=(
            "Tokyo metropolis cyber grid with deep navy (236), bright cyan (51), electric "
            "magenta (198), Japanese terminal brackets (⟦ 東京 ⟧), and optical scanline dithers."
        ),
        resource="tokyo_night_storm.ans",
    ),
    BannerPreset(
        key="amber_monochrome_arcade", name="Vintage Mainframe / Warm Amber CRT", depth="256-color extended ANSI",
        description=(
            "Classic 1980s monochrome computing aura in warm amber & gold phosphor (shades 130-228), "
            "double-line mainframe framing, and retro baud status badges."
        ),
        resource="amber_monochrome_arcade.ans",
    ),
    BannerPreset(
        key="ember_crimson_gold", name="Ember Flame / Crimson & Gold", depth="256-color extended ANSI",
        description="A double-line box-drawing border on black, with red/yellow flame gradient rules.",
        resource="ember_crimson_gold.ans",
    ),
    BannerPreset(
        key="classic_terminal_blue", name="Classic BBS / Solid Blue & Cyan", depth="256-color extended ANSI",
        description="A plain bordered box on a solid blue background (cyan/gold/magenta accents).",
        resource="classic_terminal_blue.ans",
    ),
    BannerPreset(
        key="outrun_sunset_grid", name="Outrun 80s Grid / Pink-Orange-Yellow", depth="truecolor (24-bit RGB)",
        description=(
            "Synthwave high-speed vector sun silhouette, road perspective grid, and Outrun highway typography."
        ),
        resource="outrun_sunset_grid.ans",
    ),
    BannerPreset(
        key="emerald_matrix_digital_rain", name="Bioluminescent Emerald / Matrix Rain", depth="truecolor (24-bit RGB)",
        description=(
            "Organic neural rainforest canopy with cascading digital droplet vines, biocores, and mint plasma."
        ),
        resource="emerald_matrix_digital_rain.ans",
    ),
    BannerPreset(
        key="vaporwave_pastel_dream", name="Vaporwave Dream / Pastel Lilac-Peach", depth="truecolor (24-bit RGB)",
        description=(
            "Soothing pastel lilac, sky azure, and peach aesthetic with Greek marble columns and Japanese typography."
        ),
        resource="vaporwave_pastel_dream.ans",
    ),
    BannerPreset(
        key="crimson_samurai_cyber", name="Cyber Samurai / Crimson & Gold Leaf", depth="truecolor (24-bit RGB)",
        description=(
            "Kyoto cyber-citadel aesthetic with katana blade divider, torii gate architecture, and crimson sun."
        ),
        resource="crimson_samurai_cyber.ans",
    ),
    BannerPreset(
        key="glacier_aurora_ice", name="Glacial Crystal / Arctic Cyan-White", depth="truecolor (24-bit RGB)",
        description=(
            "Sub-zero cryogenic polar vault theme with crystalline snowflake fractals and frost diamond framing."
        ),
        resource="glacier_aurora_ice.ans",
    ),
    BannerPreset(
        key="gruvbox_warm_retro", name="Gruvbox Retro / Warm Yellow-Orange", depth="256-color extended ANSI",
        description=(
            "Warm earthy retro terminal palette with typewriter block headers, cozy mechanical borders, and signal meters."
        ),
        resource="gruvbox_warm_retro.ans",
    ),
    BannerPreset(
        key="solarized_dark_cyan", name="Solarized Dark / Scientific Cyan-Blue", depth="256-color extended ANSI",
        description=(
            "Precision IEEE engineering standard with crisp drafting borders, deterministic states, and clean alignment."
        ),
        resource="solarized_dark_cyan.ans",
    ),
    BannerPreset(
        key="catppuccin_mocha_lavender", name="Catppuccin Mocha / Lavender & Mauve", depth="256-color extended ANSI",
        description=(
            "Cozy pastel dark mode with mocha crust, lavender typography, pastel pill badges, and soft dither ribbon."
        ),
        resource="catppuccin_mocha_lavender.ans",
    ),
    BannerPreset(
        key="monokai_pro_vivid", name="Monokai Pro / Vivid Hacker Syntax", depth="256-color extended ANSI",
        description=(
            "High-contrast code syntax aesthetic with vivid pink, lime green, yellow keywords, and JSON status headers."
        ),
        resource="monokai_pro_vivid.ans",
    ),
    BannerPreset(
        key="c64_nostalgia_cyan", name="Commodore 64 / 8-Bit Vintage Blue", depth="256-color extended ANSI",
        description=(
            "Authentic 1982 C64 startup screen recreation with PETSCII borders, 38911 bytes free prompt, and blinking cursor."
        ),
        resource="c64_nostalgia_cyan.ans",
    ),
    BannerPreset(
        key="cathedral_of_signals", name="Cathedral of Signals / Rose-Window Spectrum",
        depth="truecolor (24-bit RGB)",
        description=(
            "A luminous packet rose-window, soaring signal arches, jewel-tone stained-glass "
            "gradients, and a dramatic node gateway rendered as terminal architecture."
        ),
        resource="cathedral_of_signals.ans",
    ),
)

MAIN_MENU_BANNER_PRESETS: tuple[BannerPreset, ...] = (
    BannerPreset(
        key="neon_magenta_cyan", name="Neon Horizon Strip / Magenta-Cyan", depth="truecolor (24-bit RGB)",
        description=(
            "A 24-bit Truecolor gradient horizontal banner (6 lines) with a glowing \"NETBBS\" "
            "micro-wordmark, chromatic sunset half-block ribbon (░▒▓███▓▒░), and quick-"
            "navigation service accents."
        ),
        resource="neon_magenta_cyan.ans",
    ),
    BannerPreset(
        key="aurora_violet_emerald", name="Celestial Aurora Gateway / Violet-Emerald", depth="truecolor (24-bit RGB)",
        description=(
            "An astral violet-to-emerald gradient masthead (6 lines) with double-line brackets "
            "(⟦ ⟧ ═ ║), star accents (★ ✦), and live node telemetry indicators."
        ),
        resource="aurora_violet_emerald.ans",
    ),
    BannerPreset(
        key="deep_ocean_sapphire_aqua", name="Abyssal Crest / Sapphire & Aqua", depth="truecolor (24-bit RGB)",
        description=(
            "Deep ocean oceanic header (6 lines) with sapphire-to-aqua half-block dividing "
            "rules and crystal-clear service navigation."
        ),
        resource="deep_ocean_sapphire_aqua.ans",
    ),
    BannerPreset(
        key="solar_flare_gold_crimson", name="Solar Prominence / Gold & Crimson", depth="truecolor (24-bit RGB)",
        description=(
            "Supernova energy header (6 lines) with molten crimson-to-gold dither pulse bar "
            "and illuminated service badges."
        ),
        resource="solar_flare_gold_crimson.ans",
    ),
    BannerPreset(
        key="amber_warm_gold", name="Retro Amber Arcade / Warm Gold", depth="256-color extended ANSI",
        description=(
            "Warm copper and gold phosphor tones (shades 130-228, 6 lines), a segmented amber "
            "ribbon, and retro arcade-styled service badges."
        ),
        resource="amber_warm_gold.ans",
    ),
    BannerPreset(
        key="nord_frost_ice", name="Nordic Ice Clean / Slate & Frost", depth="256-color extended ANSI",
        description=(
            "A distraction-free minimalist header (5 lines) with rounded framing (╭─╮), ice "
            "cyan text, and neat diamond divider rules (◇ ── ◇)."
        ),
        resource="nord_frost_ice.ans",
    ),
    BannerPreset(
        key="matrix_phosphor_green", name="Matrix Cyber Relay / Phosphor Green", depth="256-color extended ANSI",
        description=(
            "Matrix green phosphor header (6 lines) with technical square brackets, scanline "
            "optical dithering, and cyber green navigation tags."
        ),
        resource="matrix_phosphor_green.ans",
    ),
    BannerPreset(
        key="outrun_sunset_strip", name="Outrun Sunset Strip / Pink-Orange", depth="truecolor (24-bit RGB)",
        description="6-line high-speed synthwave horizon with vector sunset ribbon and turbo navigation tags.",
        resource="outrun_sunset_strip.ans",
    ),
    BannerPreset(
        key="emerald_matrix_strip", name="Bio-Emerald Masthead / Jade-Mint", depth="truecolor (24-bit RGB)",
        description="6-line bioluminescent rainforest stream with neural telemetry badges and half-block emerald divider.",
        resource="emerald_matrix_strip.ans",
    ),
    BannerPreset(
        key="vaporwave_pastel_strip", name="Vaporwave Pastel Strip / Lilac-Sky", depth="truecolor (24-bit RGB)",
        description="6-line aesthetic Japanese pastel header with marble checkerboard rule and breezy typography.",
        resource="vaporwave_pastel_strip.ans",
    ),
    BannerPreset(
        key="crimson_samurai_strip", name="Cyber Samurai Blade / Crimson-Gold", depth="truecolor (24-bit RGB)",
        description="6-line shadow relay header with torii gate icons, katana blade ribbon, and dojo navigation.",
        resource="crimson_samurai_strip.ans",
    ),
    BannerPreset(
        key="solarized_technical_header", name="Solarized Technical / Clean Cyan", depth="256-color extended ANSI",
        description="5-line precision drafting header with IEEE telemetry badges and compact service grid.",
        resource="solarized_technical_header.ans",
    ),
    BannerPreset(
        key="catppuccin_mocha_header", name="Catppuccin Mocha Header / Lavender", depth="256-color extended ANSI",
        description="6-line cozy pastel header with lavender-to-mauve dither ribbon and rounded dark mode pill badges.",
        resource="catppuccin_mocha_header.ans",
    ),
    BannerPreset(
        key="monokai_pro_header", name="Monokai Pro Header / Vivid Code", depth="256-color extended ANSI",
        description="6-line syntax-highlighted code header with JSON status block and monospace navigation prompts.",
        resource="monokai_pro_header.ans",
    ),
    BannerPreset(
        key="c64_vintage_header", name="Commodore 64 Masthead / Vintage Blue", depth="256-color extended ANSI",
        description="5-line authentic 8-bit PETSCII header with solid C64 blue background and retro command prompt.",
        resource="c64_vintage_header.ans",
    ),
    BannerPreset(
        key="quantum_prism", name="Quantum Prism / Spectral Glass",
        depth="truecolor (24-bit RGB)",
        description=(
            "A faceted spectral-glass wordmark with ultraviolet-to-gold refraction, compact "
            "service glyphs, and an asymmetric six-line silhouette."
        ),
        resource="quantum_prism.ans",
    ),
)


BOARD_LIST_MASTHEAD_PRESETS: tuple[BannerPreset, ...] = (
    BannerPreset(
        key="aurora_violet_emerald", name="Constellation Exchange / Violet-Emerald",
        depth="truecolor (24-bit RGB)",
        description="Message constellations connected by luminous reply paths around a compact board index.",
        resource="aurora_violet_emerald.ans",
    ),
    BannerPreset(
        key="cyberpunk_sunset_gold", name="Megacity Bulletin Wall / Sunset Gold",
        depth="truecolor (24-bit RGB)",
        description="A stacked neon skyline whose lit windows become boards, posts, and live mesh routes.",
        resource="cyberpunk_sunset_gold.ans",
    ),
    BannerPreset(
        key="matrix_phosphor_green", name="Thread Matrix / Phosphor Green",
        depth="256-color extended ANSI",
        description="Branching discussion threads rendered as a crisp green routing tree and activity meter.",
        resource="matrix_phosphor_green.ans",
    ),
    BannerPreset(
        key="c64_nostalgia_cyan", name="C64 Message Base / Vintage Blue",
        depth="256-color extended ANSI",
        description="An unmistakable Commodore directory screen with numbered message bases and a READY prompt.",
        resource="c64_nostalgia_cyan.ans",
    ),
)


FILE_AREA_MASTHEAD_PRESETS: tuple[BannerPreset, ...] = (
    BannerPreset(
        key="deep_ocean_sapphire_teal", name="Abyssal Archive / Sapphire-Teal",
        depth="truecolor (24-bit RGB)",
        description="A sonar-lit submarine archive with descending shelves and bioluminescent file beacons.",
        resource="deep_ocean_sapphire_teal.ans",
    ),
    BannerPreset(
        key="glacier_aurora_ice", name="Crystal Data Vault / Arctic Cyan",
        depth="truecolor (24-bit RGB)",
        description="A fractured ice-vault silhouette with crystalline facets surrounding the file index.",
        resource="glacier_aurora_ice.ans",
    ),
    BannerPreset(
        key="gruvbox_warm_retro", name="Card Catalogue / Warm Gruvbox",
        depth="256-color extended ANSI",
        description="A tactile amber card catalogue with drawers, index tabs, and an old-library archive rhythm.",
        resource="gruvbox_warm_retro.ans",
    ),
    BannerPreset(
        key="c64_nostalgia_cyan", name="Disk Directory / C64 Blue",
        depth="256-color extended ANSI",
        description="A compact LOAD-and-LIST disk directory that turns the file-area picker into a retro drive.",
        resource="c64_nostalgia_cyan.ans",
    ),
)


CHAT_CHANNEL_PICKER_MASTHEAD_PRESETS: tuple[BannerPreset, ...] = (
    BannerPreset(
        key="synthwave_magenta_cyan", name="Neon Voiceprint / Magenta-Cyan",
        depth="truecolor (24-bit RGB)",
        description="A bright live-audio waveform crossing a sunset horizon with room activity pulses.",
        resource="synthwave_magenta_cyan.ans",
    ),
    BannerPreset(
        key="aurora_violet_emerald", name="Conversation Constellations / Aurora",
        depth="truecolor (24-bit RGB)",
        description="Channel nodes orbit across an aurora field, connected as a small social star chart.",
        resource="aurora_violet_emerald.ans",
    ),
    BannerPreset(
        key="matrix_phosphor_green", name="Signal Multiplexer / Phosphor Green",
        depth="256-color extended ANSI",
        description="Four live carriers converge through a central multiplex bus with scanline telemetry.",
        resource="matrix_phosphor_green.ans",
    ),
    BannerPreset(
        key="vaporwave_pastel_dream", name="Dreamwave Lounge / Pastel",
        depth="truecolor (24-bit RGB)",
        description="Floating speech bubbles and a checkerboard cloudline in lilac, peach, and sky blue.",
        resource="vaporwave_pastel_dream.ans",
    ),
    BannerPreset(
        key="orbital_comms", name="Orbital Comms / Electric Cyan",
        depth="truecolor (24-bit RGB)",
        description="A satellite ring, pulsing channel orbits, and a luminous carrier lock across five compact rows.",
        resource="orbital_comms.ans",
    ),
)


LOGOFF_BANNER_PRESETS: tuple[BannerPreset, ...] = (
    BannerPreset(
        key="synthwave_magenta_cyan", name="Neon Sunset / Carrier Down",
        depth="truecolor (24-bit RGB)",
        description="The mesh road falls into a magenta sunset while the caller's carrier fades cleanly away.",
        resource="synthwave_magenta_cyan.ans",
    ),
    BannerPreset(
        key="deep_ocean_sapphire_teal", name="Deep Dive / Quiet Waters",
        depth="truecolor (24-bit RGB)",
        description="A small submersible descends beneath the last cyan signal rings into a calm dark ocean.",
        resource="deep_ocean_sapphire_teal.ans",
    ),
    BannerPreset(
        key="amber_monochrome_arcade", name="Carrier Drop / Amber CRT",
        depth="256-color extended ANSI",
        description="A warm diagnostic CRT winds the baud meter to zero and closes the terminal session.",
        resource="amber_monochrome_arcade.ans",
    ),
    BannerPreset(
        key="c64_nostalgia_cyan", name="READY. / Commodore Farewell",
        depth="256-color extended ANSI",
        description="A playful C64 signoff program returns the caller to a blinking READY prompt.",
        resource="c64_nostalgia_cyan.ans",
    ),
    BannerPreset(
        key="last_light_express", name="Last Light Express / Rose-Gold Night",
        depth="truecolor (24-bit RGB)",
        description="A luminous night train carries the final packet beyond the horizon until the next call.",
        resource="last_light_express.ans",
    ),
)


NEW_ACCOUNT_BANNER_BEFORE_PRESETS: tuple[BannerPreset, ...] = (
    BannerPreset(
        key="aurora_violet_emerald", name="Aurora Identity Gate / Violet-Emerald",
        depth="truecolor (24-bit RGB)",
        description="A constellation gate frames the three steps from callsign to verified new identity.",
        resource="aurora_violet_emerald.ans",
    ),
    BannerPreset(
        key="cyberpunk_sunset_gold", name="Citizen Access Portal / Neon Gold",
        depth="truecolor (24-bit RGB)",
        description="A high-energy city checkpoint opens an identity lane toward the federated mesh.",
        resource="cyberpunk_sunset_gold.ans",
    ),
    BannerPreset(
        key="nord_frost_slate", name="Polar Identity Briefing / Frost",
        depth="256-color extended ANSI",
        description="A calm, minimal briefing card makes the upcoming signup steps exceptionally clear.",
        resource="nord_frost_slate.ans",
    ),
    BannerPreset(
        key="c64_nostalgia_cyan", name="NEW USER Generator / C64 Blue",
        depth="256-color extended ANSI",
        description="A friendly BASIC program invites the caller to initialize a new user record.",
        resource="c64_nostalgia_cyan.ans",
    ),
)


NEW_ACCOUNT_BANNER_AFTER_PRESETS: tuple[BannerPreset, ...] = (
    BannerPreset(
        key="solar_flare_crimson_amber", name="Identity Ignition / Solar Gold",
        depth="truecolor (24-bit RGB)",
        description="A brilliant stellar ignition celebrates the new account joining the carrier mesh.",
        resource="solar_flare_crimson_amber.ans",
    ),
    BannerPreset(
        key="vaporwave_pastel_dream", name="Pastel Arrival / Dreamwave",
        depth="truecolor (24-bit RGB)",
        description="Soft marble steps, rising palms, and a pastel sun welcome the caller into the community.",
        resource="vaporwave_pastel_dream.ans",
    ),
    BannerPreset(
        key="glacier_aurora_ice", name="Crystal Activation / Arctic Cyan",
        depth="truecolor (24-bit RGB)",
        description="An identity crystal blooms into a symmetric snowflake when provisioning completes.",
        resource="glacier_aurora_ice.ans",
    ),
    BannerPreset(
        key="matrix_phosphor_green", name="Account Provisioned / Matrix Green",
        depth="256-color extended ANSI",
        description="A precise terminal transaction resolves into a bold ACCESS GRANTED confirmation.",
        resource="matrix_phosphor_green.ans",
    ),
)


def _load_preset(directory: str, preset: BannerPreset) -> bytes:
    return (resources.files(__package__) / directory / preset.resource).read_bytes()


def load_welcome_banner_preset(preset: BannerPreset) -> bytes:
    return _load_preset("welcome", preset)


def load_main_menu_banner_preset(preset: BannerPreset) -> bytes:
    return _load_preset("masthead", preset)


def load_board_list_masthead_preset(preset: BannerPreset) -> bytes:
    return _load_preset("board_list", preset)


def load_file_area_masthead_preset(preset: BannerPreset) -> bytes:
    return _load_preset("file_area", preset)


def load_chat_channel_picker_masthead_preset(preset: BannerPreset) -> bytes:
    return _load_preset("chat_channel_picker", preset)


def load_logoff_banner_preset(preset: BannerPreset) -> bytes:
    return _load_preset("logoff", preset)


def load_new_account_banner_before_preset(preset: BannerPreset) -> bytes:
    return _load_preset("new_account_before", preset)


def load_new_account_banner_after_preset(preset: BannerPreset) -> bytes:
    return _load_preset("new_account_after", preset)

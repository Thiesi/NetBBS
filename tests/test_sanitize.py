"""
Tests for netbbs.rendering.sanitize (design doc round 29, issue #8).

Covers exactly the acceptance-criteria categories the issue names: ESC,
C0/C1 controls, OSC sequences, bidi controls, and ordinary UTF-8 text.
"""

from __future__ import annotations

from netbbs.rendering.sanitize import sanitize_text


# -- ordinary text is unaffected ---------------------------------------------


def test_ordinary_ascii_text_is_unchanged():
    assert sanitize_text("General discussion board") == "General discussion board"


def test_ordinary_utf8_text_is_unchanged():
    assert sanitize_text("Müller café 日本語 emoji 🎉") == "Müller café 日本語 emoji 🎉"


def test_empty_string_stays_empty():
    assert sanitize_text("") == ""


# -- C0 controls, including ESC ----------------------------------------------


def test_esc_byte_is_removed():
    assert sanitize_text("a\x1bb") == "ab"


def test_all_c0_controls_except_tab_and_newline_are_removed():
    c0 = "".join(chr(code) for code in range(0x00, 0x20))
    result = sanitize_text(c0)
    assert result == "\t"  # only tab survives at the default allow_newlines=False


def test_c0_controls_with_newlines_allowed_keeps_tab_and_newline():
    c0 = "".join(chr(code) for code in range(0x00, 0x20))
    result = sanitize_text(c0, allow_newlines=True)
    assert result == "\t\n"


def test_del_byte_is_removed():
    assert sanitize_text("a\x7fb") == "ab"


# -- C1 controls ---------------------------------------------------------


def test_all_c1_controls_are_removed():
    c1 = "".join(chr(code) for code in range(0x80, 0xA0))
    assert sanitize_text(c1) == ""


def test_c1_control_mid_text_is_removed():
    assert sanitize_text("a\x9bb") == "ab"  # U+009B: single-byte CSI


# -- OSC-shaped sequences (ESC ] ... BEL) -------------------------------------


def test_osc_sequence_is_neutralized():
    # A real OSC 0 (set window title) sequence: ESC ] 0 ; <title> BEL
    hostile = "before\x1b]0;pwned\x07after"
    result = sanitize_text(hostile)
    assert "\x1b" not in result
    assert "\x07" not in result
    assert result == "before]0;pwnedafter"


def test_csi_sequence_is_neutralized():
    # A real CSI sequence: ESC [ 2 J (clear screen)
    hostile = "before\x1b[2Jafter"
    result = sanitize_text(hostile)
    assert "\x1b" not in result
    assert result == "before[2Jafter"


def test_single_byte_csi_c1_form_is_neutralized():
    # C1's single-byte CSI (U+009B) is an alternate encoding of ESC [
    # some terminals accept -- must be caught even without a raw ESC.
    hostile = "before\x9b2Jafter"
    result = sanitize_text(hostile)
    assert "\x9b" not in result


# -- bidi controls ---------------------------------------------------------


def test_all_nine_bidi_controls_are_removed():
    bidi_code_points = [0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069]
    bidi_text = "".join(chr(code) for code in bidi_code_points)
    assert sanitize_text(bidi_text) == ""


def test_rlo_mid_text_is_removed():
    # RLO (U+202E) is the classic "reverse the rest of the visible text"
    # trick -- must not survive sanitization.
    hostile = "safe" + chr(0x202E) + "evil"
    result = sanitize_text(hostile)
    assert chr(0x202E) not in result
    assert result == "safeevil"


def test_non_bidi_format_characters_are_left_alone():
    """Confirms the deliberately narrow scope (the 9 explicit bidi
    controls, not the whole Cf category) -- zero-width joiner has
    legitimate uses (e.g. some emoji sequences) and isn't part of the
    threat model this sanitizer targets."""
    zwj = chr(0x200D)
    assert sanitize_text(f"a{zwj}b") == f"a{zwj}b"


# -- newline/tab exceptions ---------------------------------------------------


def test_tab_is_always_kept():
    assert sanitize_text("a\tb") == "a\tb"
    assert sanitize_text("a\tb", allow_newlines=True) == "a\tb"


def test_newline_dropped_by_default():
    assert sanitize_text("line one\nline two") == "line oneline two"


def test_newline_kept_when_allowed():
    assert sanitize_text("line one\nline two", allow_newlines=True) == "line one\nline two"


def test_carriage_return_always_dropped_even_with_newlines_allowed():
    assert sanitize_text("a\rb", allow_newlines=True) == "ab"
    assert sanitize_text("a\r\nb", allow_newlines=True) == "a\nb"


# -- combined/realistic hostile payloads --------------------------------------


def test_realistic_hostile_post_subject():
    hostile = "Free stuff\x1b]0;YOU HAVE BEEN HACKED\x07\x1b[2J\x1b[H"
    result = sanitize_text(hostile)
    assert result == "Free stuff]0;YOU HAVE BEEN HACKED[2J[H"
    assert "\x1b" not in result
    assert "\x07" not in result


def test_realistic_hostile_username_with_fake_prompt_injection():
    hostile = "alice\r\nSysOp: this is a fake message\r\nUsername: "
    result = sanitize_text(hostile)  # username field: no newlines allowed
    assert "\r" not in result
    assert "\n" not in result

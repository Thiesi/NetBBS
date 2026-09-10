"""
`FILE_ID.DIZ` extraction and description normalization (issue #463).

Three layers, tested separately because they fail differently:

- the pure text rules (`normalize_description`/`fit_description`/
  `decode_diz`), which decide what a description may contain;
- the in-process ZIP reader, driven with real ZIP files written to
  disk -- including the malformed and hostile shapes that matter more
  than the happy path;
- the external-unpacker path, driven with a *real* subprocess (a
  short Python program standing in for `lha`/`unrar`/`7z`) rather than
  a mock, since what is being tested is precisely the bounded read,
  the timeout, and the kill -- none of which a mock would exercise.
"""

from __future__ import annotations

import asyncio
import sys
import time
import zipfile

import pytest

from netbbs.files import diz


def _read(path, filename):
    return asyncio.run(diz.read_archive_description(path, filename))


def _fake_tool(script: str) -> tuple[str, ...]:
    """One entry for `_ARCHIVE_TOOLS`: a real subprocess that behaves
    the way some unpacker would, receiving the archive path and member
    name in the same argv positions a real one does."""
    return (sys.executable, "-c", script, "{archive}", "{member}")


# -- text rules ---------------------------------------------------------------


def test_normalize_strips_control_characters_and_carriage_returns():
    # ESC itself goes, which is what disarms the sequence; the ordinary
    # characters that followed it stay as text -- exactly what
    # `netbbs.rendering.sanitize.sanitize_text` does to untrusted text.
    assert diz.normalize_description("a\x1b[31mb\r\nc\x07") == "a[31mb\nc"


def test_normalize_strips_bidi_overrides():
    assert diz.normalize_description("safe‮reversed") == "safereversed"


def test_normalize_trims_blank_edges_but_keeps_interior_blank_lines():
    assert diz.normalize_description("\n\ntop\n\nbottom\n \n") == "top\n\nbottom"


def test_normalize_keeps_leading_indentation():
    # A DIZ's indentation is layout, not accidental whitespace.
    assert diz.normalize_description("   centred   \n") == "   centred"


def test_normalize_returns_none_for_nothing_printable():
    assert diz.normalize_description("") is None
    assert diz.normalize_description("   \n\t\n") is None
    assert diz.normalize_description("\x00\x01") is None


def test_fit_caps_lines_and_columns():
    fitted = diz.fit_description("\n".join(f"line {i} " + "x" * 200 for i in range(30)))
    lines = fitted.split("\n")
    assert len(lines) == diz.MAX_DESCRIPTION_LINES
    assert all(len(line) <= diz.MAX_DESCRIPTION_COLUMNS for line in lines)


def test_fit_caps_total_bytes():
    # Multi-byte characters: the cap is bytes, not characters, because
    # the Link file_descriptor limit it mirrors is measured in bytes.
    fitted = diz.fit_description("\n".join("ä" * 3000 for _ in range(4)))
    assert len(fitted.encode("utf-8")) <= diz.MAX_DESCRIPTION_BYTES


def test_decode_prefers_utf8():
    assert diz.decode_diz("café naïve".encode("utf-8")) == "café naïve"


def test_decode_falls_back_to_cp437_box_drawing():
    raw = bytes([0xC9, 0xCD, 0xBB]) + b"\r\n" + "Cool Game".encode("cp437")
    assert diz.decode_diz(raw) == "╔═╗\nCool Game"


def test_decode_of_empty_bytes_is_none():
    assert diz.decode_diz(b"") is None


# -- ZIP ----------------------------------------------------------------------


def _zip(path, members: dict[str, bytes]):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_zip_with_file_id_diz(tmp_path):
    archive = _zip(tmp_path / "game.zip", {
        "GAME.EXE": b"MZ\x00\x00",
        "FILE_ID.DIZ": b"Cool Game v1.0\r\nBy Someone\r\n",
    })
    assert _read(archive, "game.zip") == "Cool Game v1.0\nBy Someone"


def test_zip_member_name_matching_is_case_insensitive_and_ignores_directories(tmp_path):
    archive = _zip(tmp_path / "game.zip", {"docs/file_id.diz": b"nested and lowercase"})
    assert _read(archive, "game.zip") == "nested and lowercase"


def test_zip_is_recognised_by_content_not_extension(tmp_path):
    # The uploader's extension is a claim; is_zipfile is evidence.
    archive = _zip(tmp_path / "installer.exe", {"FILE_ID.DIZ": b"self-extracting"})
    assert _read(archive, "installer.exe") == "self-extracting"


def test_zip_without_a_diz_is_none(tmp_path):
    archive = _zip(tmp_path / "game.zip", {"README.TXT": b"no diz here"})
    assert _read(archive, "game.zip") is None


def test_zip_with_an_oversized_diz_member_is_none(tmp_path):
    archive = _zip(tmp_path / "game.zip", {"FILE_ID.DIZ": b"x" * (diz.MAX_DIZ_BYTES + 1)})
    assert _read(archive, "game.zip") is None


def test_zip_with_a_highly_compressible_diz_member_is_not_expanded(tmp_path):
    # 4 MiB of zeros compresses to a few kilobytes; the read is capped
    # regardless of what the central directory claims the size is.
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        handle.writestr("FILE_ID.DIZ", b"\x00" * (4 * 1024 * 1024))
    assert _read(archive, "bomb.zip") is None


def test_truncated_zip_is_not_an_error(tmp_path):
    archive = _zip(tmp_path / "game.zip", {"FILE_ID.DIZ": b"description"})
    archive.write_bytes(archive.read_bytes()[: len(archive.read_bytes()) // 2])
    assert _read(archive, "game.zip") is None


def test_plain_file_is_not_an_archive(tmp_path):
    plain = tmp_path / "notes.txt"
    plain.write_bytes(b"just a text file")
    assert _read(plain, "notes.txt") is None


def test_unknown_extension_never_runs_an_unpacker(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(diz.shutil, "which", lambda tool: ran.append(tool) or None)
    plain = tmp_path / "notes.txt"
    plain.write_bytes(b"just a text file")
    assert _read(plain, "notes.txt") is None
    assert ran == []


# -- external unpackers -------------------------------------------------------


def test_external_unpacker_output_becomes_the_description(tmp_path, monkeypatch):
    # Echoes back the archive it was pointed at, proving both the
    # archive path and the member name reach the tool as real argv.
    script = (
        "import sys;"
        "sys.stdout.write(open(sys.argv[1]).read() + '\\nmember=' + sys.argv[2])"
    )
    monkeypatch.setitem(diz._ARCHIVE_TOOLS, ".lzh", (_fake_tool(script),))
    archive = tmp_path / "game.lzh"
    archive.write_text("Cool Game v1.0")

    assert _read(archive, "game.lzh") == "Cool Game v1.0\nmember=FILE_ID.DIZ"


def test_external_unpacker_lowercase_member_variant_is_tried(tmp_path, monkeypatch):
    # A tool that only knows the lowercase member name still yields a
    # description -- the uppercase attempt printing nothing is not a
    # failure, it is the first of two candidates.
    script = (
        "import sys;"
        "sys.stdout.write('found it') if sys.argv[2] == 'file_id.diz' else sys.exit(1)"
    )
    monkeypatch.setitem(diz._ARCHIVE_TOOLS, ".lzh", (_fake_tool(script),))
    archive = tmp_path / "game.lzh"
    archive.write_text("payload")

    assert _read(archive, "game.lzh") == "found it"


def test_external_unpacker_falls_through_to_the_next_candidate(tmp_path, monkeypatch):
    silent = _fake_tool("import sys; sys.exit(9)")
    working = _fake_tool("import sys; sys.stdout.write('second tool')")
    monkeypatch.setitem(diz._ARCHIVE_TOOLS, ".rar", (silent, working))
    archive = tmp_path / "game.rar"
    archive.write_text("payload")

    assert _read(archive, "game.rar") == "second tool"


def test_missing_unpacker_is_silently_no_description(tmp_path, monkeypatch):
    monkeypatch.setattr(diz.shutil, "which", lambda tool: None)
    archive = tmp_path / "game.arj"
    archive.write_text("payload")

    assert _read(archive, "game.arj") is None


def test_external_unpacker_output_over_the_cap_is_discarded(tmp_path, monkeypatch):
    script = f"import sys; sys.stdout.write('x' * {diz.MAX_DIZ_BYTES + 1000})"
    monkeypatch.setitem(diz._ARCHIVE_TOOLS, ".lzh", (_fake_tool(script),))
    archive = tmp_path / "game.lzh"
    archive.write_text("payload")

    assert _read(archive, "game.lzh") is None


def test_hanging_unpacker_is_timed_out_and_killed(tmp_path, monkeypatch):
    monkeypatch.setattr(diz, "_SPAWN_TIMEOUT_SECONDS", 1)
    monkeypatch.setitem(
        diz._ARCHIVE_TOOLS, ".lzh", (_fake_tool("import time; time.sleep(120)"),)
    )
    archive = tmp_path / "game.lzh"
    archive.write_text("payload")

    started = time.monotonic()
    assert _read(archive, "game.lzh") is None
    # Bounded by the per-spawn timeout for both member-name variants,
    # not by the 120-second sleep: the child is killed, not waited on.
    assert time.monotonic() - started < 30


def test_total_budget_stops_trying_further_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(diz, "_SPAWN_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(diz, "_TOTAL_BUDGET_SECONDS", 1)
    slow = _fake_tool("import time; time.sleep(120)")
    never_reached = _fake_tool("import sys; sys.stdout.write('should not run')")
    monkeypatch.setitem(diz._ARCHIVE_TOOLS, ".rar", (slow, never_reached))
    archive = tmp_path / "game.rar"
    archive.write_text("payload")

    assert _read(archive, "game.rar") is None


def test_extracted_text_is_normalized_like_any_other_description(tmp_path, monkeypatch):
    # Written as bytes: a real unpacker copies the member out verbatim,
    # and stdout text mode would rewrite the CRLFs this is about.
    script = "import sys; sys.stdout.buffer.write(b'\\r\\n\\r\\n  art  \\x07\\r\\n' + b'y\\r\\n' * 40)"
    monkeypatch.setitem(diz._ARCHIVE_TOOLS, ".lzh", (_fake_tool(script),))
    archive = tmp_path / "game.lzh"
    archive.write_text("payload")

    description = _read(archive, "game.lzh")
    assert description.startswith("  art\ny")
    assert len(description.split("\n")) == diz.MAX_DESCRIPTION_LINES


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource limits only")
def test_posix_extraction_runs_behind_the_rlimit_launcher(tmp_path, monkeypatch):
    # The tool must come up with the launcher's ceilings already
    # applied -- proven by asking the child itself what its limits are,
    # rather than by inspecting the argv this module builds.
    script = (
        "import resource, sys;"
        "sys.stdout.write(str(resource.getrlimit(resource.RLIMIT_CPU)[0]))"
    )
    monkeypatch.setitem(diz._ARCHIVE_TOOLS, ".lzh", (_fake_tool(script),))
    archive = tmp_path / "game.lzh"
    archive.write_text("payload")

    assert _read(archive, "game.lzh") == str(diz._EXTRACT_CPU_SECONDS)


def test_zip_member_stored_with_a_dos_backslash_path_is_found(tmp_path):
    # Some DOS-era archivers stored backslashes where the ZIP spec asks
    # for forward slashes; the member is still a FILE_ID.DIZ.
    archive = tmp_path / "game.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("DOCS\\FILE_ID.DIZ", b"stored the DOS way")
    assert _read(archive, "game.zip") == "stored the DOS way"


# -- review follow-ups --------------------------------------------------------


def test_central_directory_size_is_read_without_parsing_it(tmp_path):
    archive = _zip(tmp_path / "many.zip", {f"file{i}.txt": b"x" for i in range(25)})
    declared = diz._zip_central_directory_bytes(archive)
    # 46 bytes of fixed header per entry, plus each name.
    assert declared >= 25 * 46


def test_central_directory_size_of_a_non_zip_is_none(tmp_path):
    plain = tmp_path / "notes.txt"
    plain.write_bytes(b"not a zip at all")
    assert diz._zip_central_directory_bytes(plain) is None


def test_an_archive_with_an_oversized_central_directory_is_left_unparsed(tmp_path, monkeypatch):
    """Codex review, twice over: `ZipFile()` builds a `ZipInfo` for
    every member before anything can look for a DIZ, and the entry
    *count* in the EOCD is not what bounds that work -- CPython walks
    `size_cd` bytes and never consults the count, so a crafted archive
    can understate it freely. The byte count is what has to be
    checked."""
    members = {f"pad{i}.txt": b"x" for i in range(10)}
    members["FILE_ID.DIZ"] = b"never read"
    archive = _zip(tmp_path / "many.zip", members)

    monkeypatch.setattr(diz, "MAX_ZIP_CENTRAL_DIRECTORY_BYTES", 100)
    assert _read(archive, "many.zip") is None

    monkeypatch.setattr(diz, "MAX_ZIP_CENTRAL_DIRECTORY_BYTES", 1024 * 1024)
    assert _read(archive, "many.zip") == "never read"


def test_a_lying_entry_count_does_not_get_an_archive_parsed(tmp_path, monkeypatch):
    """The specific bypass: patch both EOCD entry-count fields to 1 and
    the archive would sail past an entry-count check while still
    carrying every one of its members."""
    members = {f"pad{i}.txt": b"x" for i in range(30)}
    members["FILE_ID.DIZ"] = b"never read"
    archive = _zip(tmp_path / "liar.zip", members)
    raw = bytearray(archive.read_bytes())
    marker = raw.rfind(b"PK")
    raw[marker + 8:marker + 12] = (1).to_bytes(2, "little") * 2  # entries on disk, total
    archive.write_bytes(bytes(raw))

    monkeypatch.setattr(diz, "MAX_ZIP_CENTRAL_DIRECTORY_BYTES", 100)
    assert _read(archive, "liar.zip") is None


def test_output_from_a_failed_unpacker_is_ignored(tmp_path, monkeypatch):
    """Codex review: an unpacker that streams a damaged member and then
    reports the CRC error, or writes a diagnostic to stdout, must not
    have that taken as a description -- nor block the next candidate."""
    failing = _fake_tool("import sys; sys.stdout.write('partial garbage'); sys.exit(2)")
    working = _fake_tool("import sys; sys.stdout.write('the real one')")
    monkeypatch.setitem(diz._ARCHIVE_TOOLS, ".rar", (failing, working))
    archive = tmp_path / "game.rar"
    archive.write_text("payload")

    assert _read(archive, "game.rar") == "the real one"


def test_unicode_line_separators_are_normalized_to_newlines():
    """`str.splitlines()` breaks on U+2028/U+2029 and `split("\n")`
    does not, so leaving them in place let a DIZ be fitted to ten
    "lines" here and then rejected as more than ten by
    `validate_description` -- failing an upload (Codex review)."""
    from netbbs.files.entries import validate_description

    raw = "\u2028".join(f"line {i}" for i in range(40)).encode("utf-8")
    fitted = diz.decode_diz(raw)

    assert len(fitted.splitlines()) == diz.MAX_DESCRIPTION_LINES
    assert validate_description(fitted) == fitted  # never raises

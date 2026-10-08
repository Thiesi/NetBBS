"""
The www.netbbs.org node page generator (issue #1165 step 3, design doc
§8.13): which pages exist, what they show and never show, hostile remote
text, and replacing the site whole. The registrations source is a real
managed-DNS service database.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from services.managed_dns.store import Database as RegistrationsDatabase
from services.managed_dns.store import insert_registration, mark_matured, mark_released, revoke_registration
from services.node_pages.__main__ import main
from services.node_pages.build import (
    ACTIVE,
    LEFT,
    QUIET,
    Registration,
    SourceError,
    build,
    load_node_map,
    load_registrations,
    render_index,
    render_node_page,
    select_pages,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
FP_A = "a" * 32
FP_B = "b" * 32
FP_C = "c" * 32


def _reg(name, fingerprint=FP_A, status="matured", created="2026-09-01T00:00:00+00:00"):
    return Registration(name=name, fingerprint=fingerprint, status=status,
                        created_at=datetime.fromisoformat(created))


def _node(fingerprint=FP_A, *, name="Nib & Quill", source="met", page="shown", heard=NOW - timedelta(hours=2),
          first="2026-09-28T10:00:00+00:00", dial_in=("telnet://nibandquill.netbbs.org:23",)):
    return {
        "fingerprint": fingerprint, "friendly_name": name, "dns_name": None, "source": source,
        "first_contact_at": first, "last_heard_at": heard.isoformat() if heard else None,
        "dial_in": list(dial_in), "node_page": page,
    }


def _one(registrations, nodes):
    pages = select_pages(registrations, nodes, NOW)
    assert len(pages) == 1
    return pages[0]


# -- which pages exist -------------------------------------------------------------


def test_a_matured_name_met_by_reliable_link_gets_a_page():
    page = _one([_reg("nibandquill")], [_node()])
    assert page.name == "nibandquill"
    assert page.friendly_name == "Nib & Quill"
    assert page.state == ACTIVE
    assert page.indexed is False
    assert page.registered_at == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert page.known_since == datetime(2026, 9, 28, 10, tzinfo=timezone.utc)


@pytest.mark.parametrize("source", ["introduced", "origin", "candidate", None])
def test_only_a_node_reliable_link_met_gets_a_page(source):
    assert select_pages([_reg("far")], [_node(source=source)], NOW) == []


@pytest.mark.parametrize("page", ["off", None, "everywhere", 1])
def test_a_page_that_is_off_or_unreadable_is_not_built(page):
    assert select_pages([_reg("quiet")], [_node(page=page)], NOW) == []


def test_indexed_is_carried():
    assert _one([_reg("found")], [_node(page="indexed")]).indexed is True


@pytest.mark.parametrize("status", ["pending", "revoked", "bogus"])
def test_pending_and_revoked_names_have_no_page(status):
    assert select_pages([_reg("held", status=status)], [_node()], NOW) == []


def test_a_name_held_by_another_fingerprint_is_not_this_nodes():
    assert select_pages([_reg("other", fingerprint=FP_B)], [_node(FP_A)], NOW) == []


@pytest.mark.parametrize("name", ["../etc", "Upper", "-dash", "dash-", "a" * 64, "dot.ted", "", "sp ace"])
def test_a_name_that_is_not_one_dns_label_is_refused(name):
    assert select_pages([_reg(name)], [_node()], NOW) == []


@pytest.mark.parametrize("fingerprint", ["../../x", "A" * 32, "a" * 31, 7])
def test_a_malformed_fingerprint_in_the_map_is_ignored(fingerprint):
    assert select_pages([_reg("odd", fingerprint=str(fingerprint))], [_node(fingerprint)], NOW) == []


def test_after_a_rename_the_page_is_at_the_new_name_and_keeps_the_first_date():
    registrations = [
        _reg("oldname", status="released", created="2026-08-01T00:00:00+00:00"),
        _reg("newname", status="matured", created="2026-09-20T00:00:00+00:00"),
    ]
    page = _one(registrations, [_node()])
    assert page.name == "newname"
    assert page.registered_at == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert page.state == ACTIVE


def test_a_released_name_shows_the_node_has_left():
    assert _one([_reg("gone", status="released")], [_node()]).state == LEFT


def test_an_abandoned_name_keeps_its_page_by_last_heard():
    page = _one([_reg("away", status="abandoned")], [_node(heard=NOW - timedelta(days=10))])
    assert page.state == QUIET


@pytest.mark.parametrize("age, state", [
    (timedelta(days=7), ACTIVE), (timedelta(days=7, seconds=1), QUIET),
    (timedelta(days=30), QUIET), (timedelta(days=30, seconds=1), LEFT),
])
def test_the_state_follows_last_heard(age, state):
    assert _one([_reg("aging")], [_node(heard=NOW - age)]).state == state


def test_never_heard_reads_as_left():
    assert _one([_reg("silent")], [_node(heard=None)]).state == LEFT


def test_a_name_released_and_taken_by_another_node_starts_fresh():
    """The registrations table keys on the name, so the new holder's row
    replaces the old one's: the page follows the new fingerprint with its
    own dates, and the old node, holding no name, has none."""
    registrations = [_reg("passed", fingerprint=FP_B, created="2026-10-01T00:00:00+00:00")]
    nodes = [_node(FP_A, name="Old Board", first="2026-01-01T00:00:00+00:00"),
             _node(FP_B, name="New Board", first="2026-10-02T00:00:00+00:00")]
    page = _one(registrations, nodes)
    assert page.fingerprint == FP_B
    assert page.friendly_name == "New Board"
    assert page.member_since == datetime(2026, 10, 1, tzinfo=timezone.utc)


# -- what a page shows -------------------------------------------------------------


def test_hostile_text_is_escaped_and_unsafe_links_dropped():
    node = _node(
        name="<script>alert(1)</script>",
        dial_in=(
            "javascript:alert(1)", "https://ok.example.org/\" onmouseover=\"x", "telnet://bbs.example.org:23",
            "ssh://bbs.example.org:22", "https://bbs.example.org/<b>", "http://plain.example.org/",
            "https://www.netbbs.org@attacker.example/", "telnet://user:pw@bbs.example.org:23",
            "telnet://bbs.example.org:99999",
        ),
    )
    text = render_node_page(_one([_reg("hostile")], [node]), NOW)
    assert "<script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "javascript:" not in text
    assert "onmouseover" not in text
    assert "<b>" not in text
    assert "http://plain" not in text
    assert "attacker.example" not in text
    assert "user:pw" not in text
    assert "99999" not in text
    assert 'href="telnet://bbs.example.org:23"' in text
    assert 'href="ssh://bbs.example.org:22"' in text


def test_control_characters_are_stripped_from_the_name():
    page = _one([_reg("ctl")], [_node(name="Bad\x1b[31mBoard‮")])
    assert page.friendly_name == "Bad[31mBoard"


def test_a_blank_name_falls_back_to_the_dns_name():
    assert _one([_reg("blank")], [_node(name="   ")]).friendly_name == "blank"


def test_the_page_shows_the_facts_and_marks_noindex():
    text = render_node_page(_one([_reg("nibandquill")], [_node()]), NOW)
    assert "nibandquill.netbbs.org" in text
    assert "Nib &amp; Quill" in text
    assert "1 September 2026" in text
    assert "28 September 2026" in text
    assert "8 October 2026 (today)" in text
    assert "aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa" in text
    assert '<meta name="robots" content="noindex">' in text
    assert '<link rel="canonical" href="https://www.netbbs.org/~nibandquill">' in text
    assert "Active on NetBBS Link" in text


def test_an_indexed_page_allows_search_engines():
    text = render_node_page(_one([_reg("found")], [_node(page="indexed")]), NOW)
    assert '<meta name="robots" content="index, follow">' in text


def test_a_page_never_shows_what_the_map_does_not_carry():
    node = dict(_node(), addresses=["http://203.0.113.9:7862"], last_known_address="198.51.100.4",
                trust={"content_conduct": "blocked"})
    text = render_node_page(_one([_reg("private")], [node]), NOW)
    assert "203.0.113.9" not in text
    assert "198.51.100.4" not in text
    assert "blocked" not in text


def test_the_index_lists_every_page_and_stays_out_of_search_results():
    pages = select_pages(
        [_reg("zeta", FP_A), _reg("alpha", FP_B)],
        [_node(FP_A, name="Zeta"), _node(FP_B, name="Alpha", page="indexed")],
        NOW,
    )
    text = render_index(pages, NOW)
    assert text.index("Alpha") < text.index("Zeta")
    assert 'href="/~alpha"' in text and 'href="/~zeta"' in text
    assert '<meta name="robots" content="noindex, follow">' in text


def test_an_empty_index_says_so():
    assert "No board has a page yet." in render_index([], NOW)


# -- the sources and the site ------------------------------------------------------


@pytest.fixture
def registrations_db(tmp_path):
    path = tmp_path / "registrations.db"
    db = RegistrationsDatabase(path)
    insert_registration(db, name="nibandquill", credential_hash="h1", node_fingerprint=FP_A, dynamic=True,
                        created_at="2026-09-01T00:00:00+00:00")
    mark_matured(db, "nibandquill", matured_at="2026-09-02T00:00:00+00:00")
    insert_registration(db, name="leaver", credential_hash="h2", node_fingerprint=FP_B, dynamic=False,
                        created_at="2026-09-03T00:00:00+00:00")
    mark_matured(db, "leaver", matured_at="2026-09-04T00:00:00+00:00")
    mark_released(db, "leaver", released_at="2026-10-01T00:00:00+00:00")
    insert_registration(db, name="takendown", credential_hash="h3", node_fingerprint=FP_C, dynamic=False,
                        created_at="2026-09-05T00:00:00+00:00")
    mark_matured(db, "takendown", matured_at="2026-09-06T00:00:00+00:00")
    revoke_registration(db, "takendown", released_at="2026-10-02T00:00:00+00:00", reason="complaint")
    db.close()
    return path


def _map_file(tmp_path, nodes):
    path = tmp_path / "map.json"
    path.write_text(json.dumps({"format": 1, "exported_at": NOW.isoformat(), "exported_by": "x", "nodes": nodes}))
    return path


def test_registrations_are_read_from_a_real_service_database_without_changing_it(registrations_db):
    before = registrations_db.read_bytes()
    rows = {r.name: r for r in load_registrations(registrations_db)}
    assert rows["nibandquill"].status == "matured"
    assert rows["leaver"].status == "released"
    assert rows["takendown"].status == "revoked"
    assert registrations_db.read_bytes() == before


def test_a_missing_or_foreign_source_is_refused(tmp_path):
    with pytest.raises(SourceError):
        load_registrations(tmp_path / "none.db")
    (tmp_path / "bad.json").write_text("{\"format\": 2, \"nodes\": []}")
    with pytest.raises(SourceError):
        load_node_map(tmp_path / "bad.json")
    (tmp_path / "broken.json").write_text("{")
    with pytest.raises(SourceError):
        load_node_map(tmp_path / "broken.json")


def test_build_writes_the_site_and_replaces_it_whole(tmp_path, registrations_db):
    out = tmp_path / "site" / "nodes"
    map_path = _map_file(tmp_path, [_node(FP_A), _node(FP_B, name="Leaver"), _node(FP_C, name="Taken Down")])

    assert build(registrations_db, map_path, out, now=NOW) == 2
    assert (out / "nibandquill" / "index.html").is_file()
    assert "Left NetBBS Link" in (out / "leaver" / "index.html").read_text(encoding="utf-8")
    assert not (out / "takendown").exists()
    assert "Nib &amp; Quill" in (out / "index.html").read_text(encoding="utf-8")

    # The leaver turns its page off: its directory goes with the next build.
    map_path = _map_file(tmp_path, [_node(FP_A), _node(FP_B, page="off")])
    assert build(registrations_db, map_path, out, now=NOW) == 1
    assert not (out / "leaver").exists()
    assert sorted(p.name for p in out.parent.iterdir()) == ["nodes"]


def test_a_source_that_fails_leaves_the_old_site_standing(tmp_path, registrations_db):
    out = tmp_path / "nodes"
    assert build(registrations_db, _map_file(tmp_path, [_node(FP_A)]), out, now=NOW) == 1
    (tmp_path / "map.json").write_text("not json")

    with pytest.raises(SourceError):
        build(registrations_db, tmp_path / "map.json", out, now=NOW)

    assert (out / "nibandquill" / "index.html").is_file()


def test_the_command_reports_and_exits(tmp_path, registrations_db, capsys):
    out = tmp_path / "nodes"
    map_path = _map_file(tmp_path, [_node(FP_A)])
    args = ["--registrations", str(registrations_db), "--node-map", str(map_path), "--out", str(out)]
    assert main(args) == 0
    assert "Wrote 1 node pages" in capsys.readouterr().out
    assert main(["--registrations", str(tmp_path / "none.db"), "--node-map", str(map_path), "--out", str(out)]) == 1
    assert "Not built" in capsys.readouterr().err
    assert (out / "nibandquill").is_dir()


# -- the member badge (step 5) -----------------------------------------------------


def test_the_badge_says_since_when_and_holds_no_node_text():
    from xml.etree import ElementTree

    from services.node_pages.build import render_badge

    page = _one([_reg("badged", created="2026-09-01T00:00:00+00:00")],
                [_node(name="<b>Evil</b> & Co", first="2026-09-28T10:00:00+00:00")])
    svg = render_badge(page)
    ElementTree.fromstring(svg)  # well-formed
    assert "member since Sep 2026 · active" in svg
    assert "Evil" not in svg
    assert "badged" not in svg


def test_the_badge_color_follows_the_state():
    from services.node_pages.build import render_badge

    left = _one([_reg("gone", status="released")], [_node()])
    assert "#6b7286" in render_badge(left)
    assert "· left" in render_badge(left)


def test_the_page_offers_the_badge_snippet_escaped(tmp_path, registrations_db):
    out = tmp_path / "nodes"
    build(registrations_db, _map_file(tmp_path, [_node(FP_A)]), out, now=NOW)
    page = (out / "nibandquill" / "index.html").read_text(encoding="utf-8")
    assert (out / "nibandquill" / "badge.svg").is_file()
    assert '<img src="/~nibandquill/badge.svg"' in page
    assert ("&lt;a href=&quot;https://www.netbbs.org/~nibandquill&quot;&gt;&lt;img "
            "src=&quot;https://www.netbbs.org/~nibandquill/badge.svg&quot;") in page

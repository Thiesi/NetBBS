"""
Verified names are frozen when a record is written (design doc §5.5).

A board post, an uploaded file or a chat line written in a resource that
displays verified names keeps what it was shown under then; a later
verification, replacement, revocation, rename or display-name change does
not rewrite it. The board, file-area, chat, Link and FTN screens each have
their own tests beside the rest of their behaviour; this file holds the
revision rules and the upgrade backfill.
"""

from __future__ import annotations

import pytest

from netbbs.attestation import attest_name, revoke_attestation, set_display_name
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_crosspost, create_post, edit_post, get_post, tombstone_post, withdraw_post
from netbbs.moderation.roles import BoardPermission, grant_permissions
from netbbs.net.board_flow import _author_display_name
from netbbs.rendering.ansi import strip_ansi
from netbbs.storage.database import Database

SHOWN = "verified_and_displayed"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def _shown(db, post, requirement=SHOWN) -> str:
    return strip_ansi(_author_display_name(db, post, name_requirement=requirement))


def test_a_post_freezes_the_display_name_and_the_attested_name(db, sysop, alice):
    board = create_board(db, "real", creator=sysop, name_requirement=SHOWN)
    set_display_name(db, alice, "Al")
    attest_name(db, alice, "Alice Smith", verifier=sysop)

    post = create_post(db, board, alice, "Hi", "there")

    assert (post.author_shown_name, post.author_verified_name) == ("Al", "Alice Smith")
    assert _shown(db, post) == "Al (=Alice Smith=)"


def test_a_board_that_inherits_the_requirement_from_its_community_freezes_too(db, sysop, alice):
    from netbbs.communities import create_community

    community = create_community(db, "Real names", default_name_requirement=SHOWN, creator=sysop)
    board = create_board(db, "inherits", creator=sysop, community_id=community.id)
    attest_name(db, alice, "Alice Smith", verifier=sysop)

    assert create_post(db, board, alice, "Hi", "there").author_verified_name == "Alice Smith"


def test_a_board_that_does_not_display_names_freezes_nothing(db, sysop, alice):
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    for requirement in (None, "verified"):
        board = create_board(db, f"b{requirement}", creator=sysop, name_requirement=requirement)
        post = create_post(db, board, alice, "Hi", "there")
        assert (post.author_shown_name, post.author_verified_name) == (None, None)


def test_a_board_that_stops_displaying_names_shows_the_plain_label(db, sysop, alice):
    board = create_board(db, "real", creator=sysop, name_requirement=SHOWN)
    set_display_name(db, alice, "Al")
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    post = create_post(db, board, alice, "Hi", "there")

    assert _shown(db, post, None) == "alice"
    assert _shown(db, post, "verified") == "alice"


def test_a_post_from_before_the_board_displayed_names_shows_its_label(db, sysop, alice):
    board = create_board(db, "late", creator=sysop)
    post = create_post(db, board, alice, "Hi", "there")
    attest_name(db, alice, "Alice Smith", verifier=sysop)

    assert _shown(db, post) == "alice"


def test_an_edit_keeps_what_the_post_was_first_shown_under(db, sysop, alice):
    """An edit is a revision of the same post by the same author: the byline
    stays the root's (it is read from the root row anyway), and every
    revision row carries the root's values so an exact revision -- in the
    moderation queue, or the version list -- reads the same."""
    board = create_board(db, "real", creator=sysop, name_requirement=SHOWN)
    post = create_post(db, board, sysop, "Hi", "there")  # unverified: the gate lets a SysOp in
    attest_name(db, sysop, "Sam Sysop", verifier=sysop)

    edited = edit_post(db, post, board, subject="Hi", body="edited", edited_by=sysop)

    assert (edited.author_shown_name, edited.author_verified_name) == ("sysop", None)
    assert "(=" not in _shown(db, edited)


def test_moderator_edits_withdrawals_and_tombstones_carry_the_root_values(db, sysop, alice):
    moderator = create_user(db, "mod", password="hunter2", user_level=10)
    board = create_board(db, "real", creator=sysop, name_requirement=SHOWN)
    grant_permissions(
        db, moderator, object_type="board", object_id=board.id,
        permissions=BoardPermission.EDIT | BoardPermission.DELETE, granted_by=sysop,
    )
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    first = create_post(db, board, alice, "One", "text")
    second = create_post(db, board, alice, "Two", "text")
    revoke_attestation(db, alice, "name", actor=sysop)

    by_moderator = edit_post(db, first, board, subject="One", body="redacted", edited_by=moderator)
    withdrawn = withdraw_post(db, second, board, withdrawn_by=alice)
    tombstoned = tombstone_post(db, by_moderator, board, tombstoned_by=moderator)

    for revision in (by_moderator, withdrawn, tombstoned):
        assert revision.author_verified_name == "Alice Smith"


def test_each_cross_post_copy_follows_its_own_board(db, sysop, alice):
    shown = create_board(db, "real", creator=sysop, name_requirement=SHOWN)
    plain = create_board(db, "plain", creator=sysop)
    attest_name(db, alice, "Alice Smith", verifier=sysop)

    copies = create_crosspost(db, [shown, plain], alice, "Both", "text")

    by_board = {get_post(db, copy.post_id).board_id: get_post(db, copy.post_id) for copy in copies}
    assert by_board[shown.id].author_verified_name == "Alice Smith"
    assert by_board[plain.id].author_shown_name is None


def test_a_deleted_authors_frozen_post_still_reads_as_written(db, sysop, alice):
    from netbbs.auth.users import delete_user

    board = create_board(db, "real", creator=sysop, name_requirement=SHOWN)
    set_display_name(db, alice, "Al")
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    post = create_post(db, board, alice, "Hi", "there")
    delete_user(db, alice, deleted_by=sysop)

    assert _shown(db, get_post(db, post.post_id)) == "Al (=Alice Smith=)"


# -- the upgrade backfill ------------------------------------------------------


def _old_database(tmp_path, monkeypatch):
    """A database one migration short of the freeze."""
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, m in enumerate(MIGRATIONS) if "verified names are frozen" in m.description)
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    return Database(tmp_path / "node.db")


def _raw_post(db, board, *, author, label, created_at, root=None, edit_of=None, ftn_inbound=0):
    post_id = f"p-{board.id}-{label}-{created_at}"
    db.connection.execute(
        """INSERT INTO posts (post_id, board_id, parent_post_id, author_user_id, author_label,
               author_fingerprint, subject, body, created_at, status, root_post_id, edit_of_post_id, ftn_inbound)
           VALUES (?, ?, NULL, ?, ?, NULL, 's', 'b', ?, 'approved', ?, ?, ?)""",
        (post_id, board.id, author, label, created_at, root or post_id, edit_of, ftn_inbound),
    )
    return post_id


def test_the_backfill_keeps_a_name_only_where_the_attestation_is_no_newer_than_the_record(tmp_path, monkeypatch):
    from netbbs.chat.channels import create_channel
    from netbbs.communities import create_community
    from netbbs.files.areas import create_file_area

    db = _old_database(tmp_path, monkeypatch)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    carol = create_user(db, "carol", password="hunter2", user_level=10)
    set_display_name(db, alice, "Al")
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    attest_name(db, carol, "Carol Jones", verifier=sysop)
    db.connection.execute("UPDATE user_attestations SET created_at = '2026-05-01T00:00:00.000000Z'")
    community = create_community(db, "Real names", default_name_requirement=SHOWN, creator=sysop)
    shown = create_board(db, "shown", creator=sysop, name_requirement=SHOWN)
    inherits = create_board(db, "inherits", creator=sysop, community_id=community.id)
    plain = create_board(db, "plain", creator=sysop, name_requirement="verified")

    before = _raw_post(db, shown, author=alice.id, label="alice", created_at="2026-04-01T00:00:00.000000Z")
    after = _raw_post(db, shown, author=alice.id, label="alice", created_at="2026-06-01T00:00:00.000000Z")
    edit_of_before = _raw_post(
        db, shown, author=alice.id, label="alice", created_at="2026-07-01T00:00:00.000000Z",
        root=before, edit_of=before,
    )
    cascaded = _raw_post(db, inherits, author=carol.id, label="carol", created_at="2026-06-01T00:00:00.000000Z")
    untouched = _raw_post(db, plain, author=alice.id, label="alice", created_at="2026-06-01T00:00:00.000000Z")
    gone = _raw_post(db, shown, author=None, label="ghost", created_at="2026-06-01T00:00:00.000000Z")
    ftn = _raw_post(
        db, shown, author=None, label="Jo@Home (1:2/3)", created_at="2026-06-01T00:00:00.000000Z", ftn_inbound=1,
    )

    area = create_file_area(db, "docs", creator=sysop, name_requirement=SHOWN)
    for file_id, created_at in (("f-before", "2026-04-01T00:00:00.000000Z"), ("f-after", "2026-06-01T00:00:00.000000Z")):
        db.connection.execute(
            """INSERT INTO files (file_id, area_id, filename, description, size_bytes, sha256, storage_path,
                   uploader_user_id, uploader_label, uploader_fingerprint, created_at, status)
               VALUES (?, ?, ?, NULL, 1, 'x', 'x', ?, 'alice', NULL, ?, 'approved')""",
            (file_id, area.id, file_id, alice.id, created_at),
        )

    channel = create_channel(db, "lounge", name_requirement=SHOWN, creator=sysop)
    for created_at in ("2026-04-01T00:00:00.000000Z", "2026-06-01T00:00:00.000000Z"):
        db.connection.execute(
            """INSERT INTO channel_messages (channel_id, kind, author_label, author_fingerprint, body, created_at,
                   author_user_id) VALUES (?, 'message', 'alice', NULL, 'hi', ?, ?)""",
            (channel.id, created_at, alice.id),
        )
    db.connection.commit()
    db.close()
    monkeypatch.undo()

    db = Database(tmp_path / "node.db")
    try:
        def frozen(post_id):
            row = db.connection.execute(
                "SELECT author_shown_name, author_verified_name FROM posts WHERE post_id = ?", (post_id,)
            ).fetchone()
            return row["author_shown_name"], row["author_verified_name"]

        # Written before the verification: the plain name, no attested one --
        # the reported bug, repaired.
        assert frozen(before) == ("Al", None)
        assert frozen(after) == ("Al", "Alice Smith")
        # An edit takes its root's values, whenever it was written.
        assert frozen(edit_of_before) == ("Al", None)
        assert frozen(cascaded) == ("carol", "Carol Jones")
        assert frozen(untouched) == (None, None)
        # No account any more, or an FTN author: rendered by label as before.
        assert frozen(gone) == (None, None)
        assert frozen(ftn) == (None, None)

        files = dict(db.connection.execute("SELECT file_id, uploader_verified_name FROM files").fetchall())
        assert files == {"f-before": None, "f-after": "Alice Smith"}
        lines = [row[0] for row in db.connection.execute(
            "SELECT author_verified_name FROM channel_messages ORDER BY created_at"
        )]
        assert lines == [None, "Alice Smith"]
    finally:
        db.close()


def test_the_backfill_freezes_a_carried_post_by_the_accepted_remote_attestation(tmp_path, monkeypatch):
    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.link.remote_attestation import (
        build_remote_attestation,
        configure_attestation_authority,
        ingest_remote_attestation,
    )
    from netbbs.link.trust import TrustSubject, register_subject

    db = _old_database(tmp_path, monkeypatch)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    board = create_board(db, "shown", creator=sysop, name_requirement=SHOWN)
    home = bootstrap_node_identity(tmp_path / "home")
    subject = TrustSubject.user(home.fingerprint, "wanda")
    register_subject(db, subject, first_accepted_at="2026-08-01T00:00:00+00:00", now_iso="2026-09-15T12:00:00+00:00")
    configure_attestation_authority(
        db, home.fingerprint, attributes=["name"], reason="peer operator", now_iso="2026-09-15T12:00:00+00:00",
    )
    ingest_remote_attestation(
        db,
        build_remote_attestation(
            home.signing_key.signing_key, issuer_fingerprint=home.fingerprint, subject=subject,
            attribute="name", attested_value="Wanda Erer", subject_opt_in=True,
            issued_at="2026-09-15T11:00:00+00:00", expires_at="2026-12-01T11:00:00+00:00",
        ),
        issuer_verify_key=home.signing_key.verify_key, now_iso="2026-09-15T12:00:00+00:00",
    )
    # Evaluated once, as reading the post did before the upgrade.
    from netbbs.link.remote_attestation import get_remote_attestation_state

    assert get_remote_attestation_state(db, subject, "name", now_iso="2026-09-15T12:00:00+00:00").accepted
    label = f"wanda@{home.fingerprint}"
    early = _raw_post(db, board, author=None, label=label, created_at="2026-09-01T00:00:00+00:00")
    late = _raw_post(db, board, author=None, label=label, created_at="2026-09-20T00:00:00+00:00")
    db.connection.commit()
    db.close()
    monkeypatch.undo()

    db = Database(tmp_path / "node.db")
    try:
        rows = {
            row["post_id"]: (row["author_shown_name"], row["author_verified_name"])
            for row in db.connection.execute("SELECT post_id, author_shown_name, author_verified_name FROM posts")
        }
        assert rows[early] == (label, None)
        assert rows[late] == (label, "Wanda Erer")
    finally:
        db.close()

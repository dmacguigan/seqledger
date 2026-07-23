import os

from seqledger import db as odb
from seqledger import ingest as oingest
from seqledger import checksums as ochecksums
from helpers import make_project, write_map_file, write_md5


def _setup_project(tmp_path):
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    rows = [
        ("i1", "i1_1.fastq.gz", "i1_2.fastq.gz", "Gadus", "U1"),
        ("i2", "i2_1.fastq.gz", "i2_2.fastq.gz", "Gadus", "U2"),
    ]
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows)
    map_file = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])
    conn = odb.connect(os.path.join(tmp_path, "cat.db"))
    odb.init_db(conn)
    oingest.ingest_map_file(conn, map_file, seqdata_root=root)
    return conn, root


FILES = ["i1_1.fastq.gz", "i1_2.fastq.gz", "i2_1.fastq.gz", "i2_2.fastq.gz"]


def _md5_entries(overrides=None, extra=None):
    overrides = overrides or {}
    entries = []
    for fn in FILES:
        entries.append((overrides.get(fn, "a" * 32), f"genohub-1_X/{fn}"))
    if extra:
        entries.extend(extra)
    return entries


def test_matching_checksums_verify_backup(tmp_path):
    conn, _ = _setup_project(tmp_path)
    store = write_md5(str(tmp_path / "store.md5"), _md5_entries())
    pdrive = write_md5(str(tmp_path / "pdrive.md5"), _md5_entries())

    summary = ochecksums.load_checksums(conn, store, pdrive)
    assert summary["matched"] == 4

    matches = conn.execute("SELECT COUNT(*) FROM files WHERE md5_match=1").fetchone()[0]
    assert matches == 4
    backup = conn.execute(
        "SELECT verified, n_files, n_mismatch FROM backups WHERE project_id='genohub-1_X'"
    ).fetchone()
    assert (backup["verified"], backup["n_files"], backup["n_mismatch"]) == (1, 4, 0)


def test_mismatch_flags_backup(tmp_path):
    conn, _ = _setup_project(tmp_path)
    store = write_md5(str(tmp_path / "store.md5"), _md5_entries())
    pdrive = write_md5(str(tmp_path / "pdrive.md5"),
                       _md5_entries(overrides={"i2_2.fastq.gz": "b" * 32}))

    summary = ochecksums.load_checksums(conn, store, pdrive)
    assert any("MISMATCH" in w for w in summary["warnings"])

    bad = conn.execute(
        "SELECT filename FROM files WHERE md5_match=0").fetchall()
    assert [r["filename"] for r in bad] == ["i2_2.fastq.gz"]
    backup = conn.execute(
        "SELECT verified, n_mismatch FROM backups WHERE project_id='genohub-1_X'").fetchone()
    assert (backup["verified"], backup["n_mismatch"]) == (0, 1)


def test_orphan_in_md5_listing_warns(tmp_path):
    conn, _ = _setup_project(tmp_path)
    extra = [("a" * 32, "genohub-1_X/stray.fastq.gz")]
    store = write_md5(str(tmp_path / "store.md5"), _md5_entries(extra=extra))
    pdrive = write_md5(str(tmp_path / "pdrive.md5"), _md5_entries(extra=extra))

    summary = ochecksums.load_checksums(conn, store, pdrive)
    assert any("orphan" in w and "stray.fastq.gz" in w for w in summary["warnings"])


def test_file_absent_from_both_listings_unverifies_backup(tmp_path):
    # #5 regression: a cataloged file that vanishes from BOTH md5 listings used
    # to keep its stale md5_match=1, leaving the backup wrongly 'verified'.
    conn, _ = _setup_project(tmp_path)

    # First, a complete pair verifies the backup.
    store = write_md5(str(tmp_path / "store.md5"), _md5_entries())
    pdrive = write_md5(str(tmp_path / "pdrive.md5"), _md5_entries())
    ochecksums.load_checksums(conn, store, pdrive)
    backup = conn.execute(
        "SELECT verified FROM backups WHERE project_id='genohub-1_X'").fetchone()
    assert backup["verified"] == 1

    # Now one file is absent from both listings (e.g. deleted before re-hashing).
    present = [e for e in _md5_entries() if not e[1].endswith("i2_2.fastq.gz")]
    store2 = write_md5(str(tmp_path / "store2.md5"), present)
    pdrive2 = write_md5(str(tmp_path / "pdrive2.md5"), present)
    summary = ochecksums.load_checksums(conn, store2, pdrive2)

    # The vanished file is reset to uncompared and flagged.
    row = conn.execute(
        "SELECT md5_match, store_md5, pdrive_md5 FROM files "
        "WHERE filename='i2_2.fastq.gz'").fetchone()
    assert row["md5_match"] is None
    assert row["store_md5"] is None and row["pdrive_md5"] is None
    assert summary["absent"] == 1
    assert any("i2_2.fastq.gz" in w and "absent from both" in w
               for w in summary["warnings"])

    # ...so the backup is no longer verified.
    backup = conn.execute(
        "SELECT verified FROM backups WHERE project_id='genohub-1_X'").fetchone()
    assert backup["verified"] != 1


def test_empty_store_listing_warns_and_leaves_status_incomplete(tmp_path):
    # #21 regression: an empty/truncated listing must not pass silently.
    conn, _ = _setup_project(tmp_path)
    store = write_md5(str(tmp_path / "store.md5"), [])  # truncated to nothing
    pdrive = write_md5(str(tmp_path / "pdrive.md5"), _md5_entries())

    summary = ochecksums.load_checksums(conn, store, pdrive)

    assert any("Store md5 listing is empty" in w for w in summary["warnings"])
    # No file can be verified with the store side missing; all stay uncompared.
    n_null = conn.execute(
        "SELECT COUNT(*) FROM files WHERE md5_match IS NULL").fetchone()[0]
    assert n_null == 4
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE md5_match=1").fetchone()[0] == 0
    backup = conn.execute(
        "SELECT verified FROM backups WHERE project_id='genohub-1_X'").fetchone()
    assert backup["verified"] != 1

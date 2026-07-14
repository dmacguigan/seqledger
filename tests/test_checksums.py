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

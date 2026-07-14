"""Auto-discovery ingest: pair project folders with <project>_mapfile.csv."""

import gzip
import os

from seqledger import db as odb
from seqledger import ingest as oingest
from helpers import make_project


def _fresh_db(tmp_path):
    conn = odb.connect(os.path.join(tmp_path, "cat.db"))
    odb.init_db(conn)
    return conn


def _roots(tmp_path):
    """seqdata + metadata roots (same dir, matching the on-disk convention)."""
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    return root, root


def _gz(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wb") as f:
        f.write(b"@r\nACGT\n+\nIIII\n")


def test_tree_ingests_folder_plus_mapfile(tmp_path):
    seq, meta = _roots(tmp_path)
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    make_project(seq, "genohub-1_X", "genohub-1_X_mapfile.csv", rows)

    conn = _fresh_db(tmp_path)
    results = oingest.ingest_tree(conn, seq, meta)

    assert [r[0] for r in results] == ["genohub-1_X"]
    assert results[0][2] == "pass"
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2
    assert conn.execute(
        "SELECT metadata_status FROM projects").fetchone()["metadata_status"] == "ok"


def test_tree_finds_nested_fastq(tmp_path):
    seq, meta = _roots(tmp_path)
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    # mapfile lists bare basenames; files live in a nested subdir on disk.
    make_project(seq, "genohub-1_X", "genohub-1_X_mapfile.csv", rows, disk_files=[])
    _gz(os.path.join(seq, "genohub-1_X", "lane1", "s1_1.fastq.gz"))
    _gz(os.path.join(seq, "genohub-1_X", "lane1", "s1_2.fastq.gz"))

    conn = _fresh_db(tmp_path)
    oingest.ingest_tree(conn, seq, meta)

    rel = conn.execute(
        "SELECT rel_path FROM files WHERE filename='s1_1.fastq.gz'").fetchone()["rel_path"]
    assert rel == os.path.join("genohub-1_X", "lane1", "s1_1.fastq.gz")
    # size captured -> file was actually located on disk
    assert conn.execute(
        "SELECT size_bytes FROM files WHERE filename='s1_1.fastq.gz'").fetchone()[0] > 0


def test_tree_missing_mapfile_catalogs_disk_files_only(tmp_path):
    seq, meta = _roots(tmp_path)
    # A project folder with fastq but NO mapfile in the metadata dir.
    _gz(os.path.join(seq, "genohub-9_Z", "a_1.fastq.gz"))
    _gz(os.path.join(seq, "genohub-9_Z", "a_2.fastq.gz"))

    conn = _fresh_db(tmp_path)
    results = oingest.ingest_tree(conn, seq, meta)

    assert results[0][3]["metadata_status"] == "missing_mapfile"
    row = conn.execute(
        "SELECT metadata_status FROM projects WHERE project_id='genohub-9_Z'").fetchone()
    assert row["metadata_status"] == "missing_mapfile"
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0
    # files cataloged with no sample and a best-effort direction guess
    files = conn.execute(
        "SELECT filename, sample_pk, direction FROM files ORDER BY filename").fetchall()
    assert [f["filename"] for f in files] == ["a_1.fastq.gz", "a_2.fastq.gz"]
    assert all(f["sample_pk"] is None for f in files)
    assert [f["direction"] for f in files] == ["R1", "R2"]


def test_tree_missing_seqdata_catalogs_samples_only(tmp_path):
    seq, meta = _roots(tmp_path)
    # A mapfile with no matching project folder on disk.
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    make_project(seq, "genohub-7_M", "genohub-7_M_mapfile.csv", rows, disk_files=[])
    # remove the (empty) folder make_project created, so no folder exists
    os.rmdir(os.path.join(seq, "genohub-7_M"))

    conn = _fresh_db(tmp_path)
    results = oingest.ingest_tree(conn, seq, meta)

    assert results[0][3]["metadata_status"] == "missing_seqdata"
    assert conn.execute(
        "SELECT metadata_status FROM projects").fetchone()["metadata_status"] == "missing_seqdata"
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1


def test_tree_broken_mapfile_flags_and_catalogs_disk(tmp_path):
    seq, meta = _roots(tmp_path)
    # Bad header (missing required columns) but a real folder with fastq.
    _gz(os.path.join(seq, "genohub-3_B", "x_1.fastq.gz"))
    with open(os.path.join(seq, "genohub-3_B_mapfile.csv"), "w") as f:
        f.write("ID,Sample,Notes\n")
        f.write("s1,foo,bar\n")

    conn = _fresh_db(tmp_path)
    results = oingest.ingest_tree(conn, seq, meta)

    assert results[0][2] == "fail"
    assert results[0][3]["metadata_status"] == "broken_mapfile"
    row = conn.execute("SELECT metadata_status, metadata_detail FROM projects").fetchone()
    assert row["metadata_status"] == "broken_mapfile"
    assert "ID,R1,R2,Taxon,UniqID" in row["metadata_detail"]
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1


def test_discover_projects_unions_both_sides(tmp_path):
    seq, meta = _roots(tmp_path)
    os.makedirs(os.path.join(seq, "onlyfolder"))
    open(os.path.join(meta, "onlymap_mapfile.csv"), "w").close()
    make_project(seq, "both", "both_mapfile.csv",
                 [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")])

    found = {p["project_id"]: p for p in oingest.discover_projects(seq, meta)}
    assert set(found) == {"onlyfolder", "onlymap", "both"}
    assert found["onlyfolder"]["mapfile"] is None
    assert found["onlymap"]["data_dir"] is None
    assert found["both"]["data_dir"] and found["both"]["mapfile"]


def test_prune_missing_projects_deletes_vanished(tmp_path):
    seq, meta = _roots(tmp_path)
    make_project(seq, "genohub-1_A", "genohub-1_A_mapfile.csv",
                 [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")])
    make_project(seq, "genohub-2_B", "genohub-2_B_mapfile.csv",
                 [("s2", "s2_1.fastq.gz", "s2_2.fastq.gz", "Gadus", "U2")])
    conn = _fresh_db(tmp_path)
    oingest.ingest_tree(conn, seq, meta)
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 2

    # B vanishes from both roots (folder + mapfile removed).
    shutil = __import__("shutil")
    shutil.rmtree(os.path.join(seq, "genohub-2_B"))
    os.remove(os.path.join(meta, "genohub-2_B_mapfile.csv"))

    res = oingest.prune_missing_projects(conn, seq, meta)
    assert res["skipped"] is False
    assert res["pruned"] == ["genohub-2_B"]
    # project + its samples/files cascade-deleted; A untouched
    assert [r["project_id"] for r in conn.execute("SELECT project_id FROM projects")] == \
        ["genohub-1_A"]
    assert conn.execute(
        "SELECT COUNT(*) FROM samples WHERE project_id='genohub-2_B'").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE project_id='genohub-2_B'").fetchone()[0] == 0


def test_prune_missing_projects_refuses_when_nothing_discovered(tmp_path):
    seq, meta = _roots(tmp_path)
    make_project(seq, "genohub-1_A", "genohub-1_A_mapfile.csv",
                 [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")])
    conn = _fresh_db(tmp_path)
    oingest.ingest_tree(conn, seq, meta)

    # Simulate an unmounted / empty root: discovery finds nothing -> must NOT prune.
    empty = str(tmp_path / "empty_root")
    os.makedirs(empty, exist_ok=True)
    res = oingest.prune_missing_projects(conn, empty, empty)
    assert res["skipped"] is True
    assert res["pruned"] == []
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1  # kept


def test_prune_missing_projects_ignores_other_roots(tmp_path):
    seq, meta = _roots(tmp_path)
    make_project(seq, "genohub-1_A", "genohub-1_A_mapfile.csv",
                 [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")])
    conn = _fresh_db(tmp_path)
    oingest.ingest_tree(conn, seq, meta)

    # A project stored under a DIFFERENT seqdata_root must not be pruned by a run
    # over this root, even though it isn't discovered here.
    conn.execute("INSERT INTO projects (project_id, seqdata_root) VALUES (?, ?)",
                 ("other-proj", "/some/other/root"))
    conn.commit()
    res = oingest.prune_missing_projects(conn, seq, meta)
    assert res["pruned"] == []  # nothing under this root vanished
    assert conn.execute(
        "SELECT COUNT(*) FROM projects WHERE project_id='other-proj'").fetchone()[0] == 1


def test_tree_reingest_upgrades_missing_mapfile_to_ok(tmp_path):
    seq, meta = _roots(tmp_path)
    _gz(os.path.join(seq, "genohub-1_X", "s1_1.fastq.gz"))
    _gz(os.path.join(seq, "genohub-1_X", "s1_2.fastq.gz"))
    conn = _fresh_db(tmp_path)
    oingest.ingest_tree(conn, seq, meta)
    assert conn.execute(
        "SELECT metadata_status FROM projects").fetchone()["metadata_status"] == "missing_mapfile"

    # Add the mapfile and re-ingest: project flips to ok, files link to the sample.
    with open(os.path.join(meta, "genohub-1_X_mapfile.csv"), "w") as f:
        f.write("ID,R1,R2,Taxon,UniqID\n")
        f.write("s1,s1_1.fastq.gz,s1_2.fastq.gz,Gadus,U1\n")
    oingest.ingest_tree(conn, seq, meta)

    assert conn.execute(
        "SELECT metadata_status FROM projects").fetchone()["metadata_status"] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
    linked = conn.execute(
        "SELECT COUNT(*) FROM files WHERE sample_pk IS NOT NULL").fetchone()[0]
    assert linked == 2

import gzip
import os

from odna import db as odb
from odna import ingest as oingest
from odna import validate as oval
from helpers import make_project, write_map_file


def _setup(tmp_path, rows, disk_files=None):
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows, disk_files=disk_files)
    map_file = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])
    conn = odb.connect(os.path.join(tmp_path, "cat.db"))
    odb.init_db(conn)
    oingest.ingest_map_file(conn, map_file, seqdata_root=root)
    return conn, root


def test_data_check_ok(tmp_path):
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    res = oval.validate_catalog(conn, seqdata_root=root)["genohub-1_X"]
    assert res["data"]["status"] == "ok"
    assert res["data"]["n_missing"] == 0 and res["data"]["n_orphan"] == 0
    # persisted
    row = conn.execute(
        "SELECT data_check_status FROM projects WHERE project_id='genohub-1_X'").fetchone()
    assert row["data_check_status"] == "ok"


def test_data_check_missing_file(tmp_path):
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    os.remove(os.path.join(root, "genohub-1_X", "s1_2.fastq.gz"))
    res = oval.validate_catalog(conn, seqdata_root=root)["genohub-1_X"]
    assert res["data"]["status"] == "issues"
    assert res["data"]["n_missing"] == 1 and res["data"]["n_orphan"] == 0
    assert "s1_2.fastq.gz" in res["data"]["missing"]


def test_data_check_orphan_file(tmp_path):
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    with gzip.open(os.path.join(root, "genohub-1_X", "extra.fastq.gz"), "wb") as f:
        f.write(b"x")
    res = oval.validate_catalog(conn, seqdata_root=root)["genohub-1_X"]
    assert res["data"]["status"] == "issues"
    assert res["data"]["n_orphan"] == 1 and "extra.fastq.gz" in res["data"]["orphan"]


def test_data_check_issues_persisted(tmp_path):
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    os.remove(os.path.join(root, "genohub-1_X", "s1_2.fastq.gz"))
    with gzip.open(os.path.join(root, "genohub-1_X", "extra.fastq.gz"), "wb") as f:
        f.write(b"x")
    oval.validate_catalog(conn, seqdata_root=root)
    got = {(r["kind"], r["filename"]) for r in conn.execute(
        "SELECT kind, filename FROM data_check_issues WHERE project_id='genohub-1_X'")}
    assert got == {("missing from disk", "s1_2.fastq.gz"),
                   ("missing from mapfile", "extra.fastq.gz")}

    # re-run after fixing -> issue rows cleared
    with gzip.open(os.path.join(root, "genohub-1_X", "s1_2.fastq.gz"), "wb") as f:
        f.write(b"x")
    os.remove(os.path.join(root, "genohub-1_X", "extra.fastq.gz"))
    oval.validate_catalog(conn, seqdata_root=root)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM data_check_issues WHERE project_id='genohub-1_X'"
    ).fetchone()["n"]
    assert n == 0


def test_checksum_status_transitions(tmp_path):
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)

    # no md5s yet -> incomplete
    assert oval.check_checksums(conn, "genohub-1_X")["status"] == "incomplete"

    conn.execute("UPDATE files SET md5_match=1 WHERE project_id='genohub-1_X'")
    assert oval.check_checksums(conn, "genohub-1_X")["status"] == "verified"

    conn.execute(
        "UPDATE files SET md5_match=0 WHERE project_id='genohub-1_X' AND direction='R2'")
    cs = oval.check_checksums(conn, "genohub-1_X")
    assert cs["status"] == "mismatch" and cs["n_mismatch"] == 1


def test_validate_without_seqdata_root_reads_stored(tmp_path):
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    oval.validate_catalog(conn, seqdata_root=root)  # persists 'ok'
    res = oval.validate_catalog(conn)["genohub-1_X"]  # no disk scan
    assert res["data"]["status"] == "ok"


def test_prune_clears_rows_dropped_from_csv(tmp_path):
    # s1 backed on disk; bad1/bad2 listed in CSV but absent on disk -> flagged missing.
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1"),
            ("bad1", "bad1_1.fastq.gz", "bad1_2.fastq.gz", "Gadus", "U2"),
            ("bad2", "bad2_1.fastq.gz", "bad2_2.fastq.gz", "Gadus", "U3")]
    root = str(tmp_path / "raw_sequence_data")
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows,
                 disk_files=["s1_1.fastq.gz", "s1_2.fastq.gz"])
    map_file = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])
    conn = odb.connect(str(tmp_path / "cat.db"))
    odb.init_db(conn)
    oingest.ingest_map_file(conn, map_file, seqdata_root=root)
    assert oval.validate_catalog(conn, seqdata_root=root)["genohub-1_X"]["data"]["n_missing"] == 4

    # user removes the two bad rows; without --prune the stale rows still flag missing
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows[:1],
                 disk_files=["s1_1.fastq.gz", "s1_2.fastq.gz"])
    oingest.ingest_map_file(conn, map_file, seqdata_root=root)
    assert oval.validate_catalog(conn, seqdata_root=root)["genohub-1_X"]["data"]["n_missing"] == 4

    # re-ingest with prune -> stale samples/files gone, project clean
    res = oingest.ingest_map_file(conn, map_file, seqdata_root=root, prune=True)
    assert res[0][3]["pruned_samples"] == ["bad1", "bad2"]
    data = oval.validate_catalog(conn, seqdata_root=root)["genohub-1_X"]["data"]
    assert data["status"] == "ok" and data["n_missing"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"] == 2


def test_prune_drops_file_renamed_in_place(tmp_path):
    # A filename typo fixed within a surviving row leaves the old file row stale.
    rows = [("s1", "s1_1.fastq.gz", "TYPO_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv",
                 [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")])
    map_file = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])
    res = oingest.ingest_map_file(conn, map_file, seqdata_root=root, prune=True)
    assert res[0][3]["pruned_files"] == 1
    got = {r["filename"] for r in conn.execute("SELECT filename FROM files")}
    assert got == {"s1_1.fastq.gz", "s1_2.fastq.gz"}

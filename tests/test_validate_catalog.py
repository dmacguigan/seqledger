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
    assert got == {("missing", "s1_2.fastq.gz"), ("orphan", "extra.fastq.gz")}

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
        "UPDATE files SET md5_match=0 WHERE project_id='genohub-1_X' AND role='R2'")
    cs = oval.check_checksums(conn, "genohub-1_X")
    assert cs["status"] == "mismatch" and cs["n_mismatch"] == 1


def test_validate_without_seqdata_root_reads_stored(tmp_path):
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    oval.validate_catalog(conn, seqdata_root=root)  # persists 'ok'
    res = oval.validate_catalog(conn)["genohub-1_X"]  # no disk scan
    assert res["data"]["status"] == "ok"

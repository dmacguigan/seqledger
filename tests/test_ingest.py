import os
import pwd

from seqledger import db as odb
from seqledger import ingest as oingest
from helpers import make_project, write_map_file


def _fresh_db(tmp_path):
    conn = odb.connect(os.path.join(tmp_path, "cat.db"))
    odb.init_db(conn)
    return conn


def test_ingest_populates_tables(tmp_path):
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    rows = [
        ("i1", "i1_1.fastq.gz", "i1_2.fastq.gz", "Gadus morhua", "USNM 1", "extraval"),
        ("i2", "i2_1.fastq.gz", "i2_2.fastq.gz", "Urophycis sp.", "USNM 2", "extraval2"),
    ]
    header = ["ID", "R1", "R2", "Taxon", "UniqID", "Notes"]
    make_project(root, "genohub-1249488_WHF2",
                 "genohub-1249488_WHF2_mapfile.csv", rows, header=header)
    map_file = write_map_file(root, [
        ("genohub-1249488_WHF2_mapfile.csv", "genohub-1249488_WHF2")])

    conn = _fresh_db(tmp_path)
    results = oingest.ingest_map_file(conn, map_file, seqdata_root=root)

    project_id, findings, status, stats = results[0]
    assert project_id == "genohub-1249488_WHF2"
    assert status == "pass", findings

    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 4
    row = conn.execute(
        "SELECT source, project_number, description FROM projects").fetchone()
    assert (row["source"], row["project_number"], row["description"]) == (
        "genohub", "1249488", "WHF2")
    # extra column captured as JSON
    extra = conn.execute(
        "SELECT extra_json FROM samples WHERE sample_id='i1'").fetchone()["extra_json"]
    assert "extraval" in extra


def test_ingest_is_idempotent(tmp_path):
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    rows = [("i1", "i1_1.fastq.gz", "i1_2.fastq.gz", "Gadus", "USNM 1")]
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows)
    map_file = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])

    conn = _fresh_db(tmp_path)
    oingest.ingest_map_file(conn, map_file, seqdata_root=root)
    oingest.ingest_map_file(conn, map_file, seqdata_root=root)

    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2


def test_ingest_captures_owner_and_size(tmp_path):
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    rows = [("i1", "i1_1.fastq.gz", "i1_2.fastq.gz", "Gadus", "USNM 1")]
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows)
    map_file = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])

    conn = _fresh_db(tmp_path)
    oingest.ingest_map_file(conn, map_file, seqdata_root=root)

    me = pwd.getpwuid(os.getuid()).pw_name
    frow = conn.execute(
        "SELECT size_bytes, owner_name FROM files WHERE filename='i1_1.fastq.gz'").fetchone()
    assert frow["size_bytes"] > 0
    assert frow["owner_name"] == me
    prow = conn.execute(
        "SELECT seqdata_root, owner_name FROM projects").fetchone()
    assert prow["seqdata_root"] == os.path.abspath(root)
    assert prow["owner_name"] == me


def test_ingest_without_seqdata_root_leaves_owner_null(tmp_path):
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    rows = [("i1", "i1_1.fastq.gz", "i1_2.fastq.gz", "Gadus", "USNM 1")]
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows)
    map_file = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])

    conn = _fresh_db(tmp_path)
    oingest.ingest_map_file(conn, map_file)  # no seqdata_root
    frow = conn.execute(
        "SELECT size_bytes, owner_name FROM files WHERE filename='i1_1.fastq.gz'").fetchone()
    assert frow["size_bytes"] is None and frow["owner_name"] is None


def test_ingest_rejects_bad_project(tmp_path):
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    # duplicate sample ID -> FAIL, project not loaded
    rows = [
        ("dup", "a.fastq.gz", "b.fastq.gz", "Gadus", "U1"),
        ("dup", "c.fastq.gz", "d.fastq.gz", "Gadus", "U2"),
    ]
    make_project(root, "genohub-2_Y", "genohub-2_Y_mapfile.csv", rows)
    map_file = write_map_file(root, [("genohub-2_Y_mapfile.csv", "genohub-2_Y")])

    conn = _fresh_db(tmp_path)
    results = oingest.ingest_map_file(conn, map_file, seqdata_root=root)
    assert results[0][2] == "fail"
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0

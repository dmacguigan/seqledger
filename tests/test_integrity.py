import gzip
import json
import os

from seqledger import db as odb
from seqledger import ingest as oingest
from seqledger import integrity as oint
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


def _write_gz(root, fn, data):
    with gzip.open(os.path.join(root, "genohub-1_X", fn), "wb") as f:
        f.write(data)


def _truncate(root, fn, n_trailing):
    p = os.path.join(root, "genohub-1_X", fn)
    with open(p, "rb") as f:
        raw = f.read()
    with open(p, "wb") as f:
        f.write(raw[:-n_trailing])


# ---- incremental skip / resumability ---------------------------------------

def test_integrity_skips_unchanged_on_rerun(tmp_path):
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    oint.check_catalog_integrity(conn, seqdata_root=root, progress=False)
    assert all(r[0] == "ok" for r in
               conn.execute("SELECT integrity_status FROM files"))

    # Count real decompress calls: a re-run must re-read nothing (all skipped),
    # and --force (recheck) must re-read every file.
    real = oint.check_fastq_gz
    calls = {"n": 0}

    def counting(path, **kw):
        calls["n"] += 1
        return real(path, **kw)

    oint.check_fastq_gz = counting
    try:
        oint.check_catalog_integrity(conn, seqdata_root=root, progress=False)
        assert calls["n"] == 0  # unchanged + already-passed -> skipped, no re-read

        calls["n"] = 0
        oint.check_catalog_integrity(conn, seqdata_root=root, progress=False,
                                     recheck=True)
        assert calls["n"] == 2  # --force re-reads both files
    finally:
        oint.check_fastq_gz = real


def test_integrity_rechecks_when_size_changes(tmp_path):
    # A size change must bypass the skip (else real corruption would be hidden).
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    oint.check_catalog_integrity(conn, seqdata_root=root, progress=False)
    assert conn.execute(
        "SELECT integrity_status FROM files WHERE filename='s1_1.fastq.gz'"
    ).fetchone()[0] == "ok"

    _truncate(root, "s1_1.fastq.gz", 4)  # shrinks size and corrupts the trailer
    oint.check_catalog_integrity(conn, seqdata_root=root, progress=False)  # no force
    assert conn.execute(
        "SELECT integrity_status FROM files WHERE filename='s1_1.fastq.gz'"
    ).fetchone()[0] == "gzip_error"


# ---- check_fastq_gz unit ----------------------------------------------------

def test_check_fastq_gz_ok(tmp_path):
    p = str(tmp_path / "a.fastq.gz")
    with gzip.open(p, "wb") as f:
        f.write(b"@r1\nACGT\n+\nIIII\n@r2\nTTTT\n+\nJJJJ\n")
    res = oint.check_fastq_gz(p)
    assert res["status"] == oint.OK and res["n_reads"] == 2


def test_check_fastq_gz_truncated(tmp_path):
    p = str(tmp_path / "a.fastq.gz")
    with gzip.open(p, "wb") as f:
        f.write(b"@r1\nACGT\n+\nIIII\n")
    with open(p, "rb") as f:
        raw = f.read()
    with open(p, "wb") as f:
        f.write(raw[:-4])  # drop part of the gzip trailer
    assert oint.check_fastq_gz(p)["status"] == oint.GZIP_ERROR


def test_check_fastq_gz_bad_header_not_flagged(tmp_path):
    # Per-record '@'/'+' framing is intentionally not checked (gzip-integrity
    # only); a well-formed, cleanly-decompressing file passes.
    p = str(tmp_path / "a.fastq.gz")
    with gzip.open(p, "wb") as f:
        f.write(b"notheader\nACGT\n+\nIIII\n")
    assert oint.check_fastq_gz(p)["status"] == oint.OK


def test_check_fastq_gz_line_count(tmp_path):
    p = str(tmp_path / "a.fastq.gz")
    with gzip.open(p, "wb") as f:
        f.write(b"@r1\nACGT\n+\n")  # 3 lines, not a multiple of 4
    assert oint.check_fastq_gz(p)["status"] == oint.FORMAT_ERROR


def test_check_fastq_gz_no_trailing_newline(tmp_path):
    p = str(tmp_path / "a.fastq.gz")
    with gzip.open(p, "wb") as f:
        f.write(b"@r1\nACGT\n+\nIIII")  # 4 lines, last has no trailing newline
    res = oint.check_fastq_gz(p)
    assert res["status"] == oint.OK and res["n_reads"] == 1


# ---- catalog driver ---------------------------------------------------------

def test_catalog_all_ok(tmp_path):
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    res = oint.check_catalog_integrity(conn, seqdata_root=root)["genohub-1_X"]
    assert res["status"] == "pass"
    assert res["n_files"] == 2 and res["n_ok"] == 2
    got = {r["direction"]: (r["integrity_status"], r["gz_ok"], r["n_reads"])
           for r in conn.execute(
               "SELECT direction, integrity_status, gz_ok, n_reads FROM files")}
    assert got["R1"] == ("ok", 1, 1) and got["R2"] == ("ok", 1, 1)
    log = conn.execute(
        "SELECT status FROM validation_log WHERE project_id='genohub-1_X'").fetchone()
    assert log["status"] == "pass"


def test_catalog_truncated_fails(tmp_path):
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    _truncate(root, "s1_2.fastq.gz", 4)
    res = oint.check_catalog_integrity(conn, seqdata_root=root)["genohub-1_X"]
    assert res["status"] == "fail" and res["n_gzip_error"] == 1
    row = conn.execute(
        "SELECT integrity_status, gz_ok FROM files WHERE direction='R2'").fetchone()
    assert row["integrity_status"] == "gzip_error" and row["gz_ok"] == 0


def test_catalog_format_error(tmp_path):
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    _write_gz(root, "s1_1.fastq.gz", b"@r1\nACGT\n+\n")  # 3 lines -> not a multiple of 4
    res = oint.check_catalog_integrity(conn, seqdata_root=root)["genohub-1_X"]
    assert res["status"] == "fail" and res["n_format_error"] == 1


def test_catalog_parity_warn(tmp_path):
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    # R1 gets two reads, R2 keeps one -> parity mismatch
    _write_gz(root, "s1_1.fastq.gz", b"@r1\nAC\n+\nII\n@r2\nGT\n+\nII\n")
    res = oint.check_catalog_integrity(conn, seqdata_root=root)["genohub-1_X"]
    assert res["status"] == "warn"
    assert res["n_ok"] == 2 and len(res["parity_warnings"]) == 1


def test_catalog_unchecked_missing_file(tmp_path):
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    os.remove(os.path.join(root, "genohub-1_X", "s1_2.fastq.gz"))
    res = oint.check_catalog_integrity(conn, seqdata_root=root)["genohub-1_X"]
    assert res["status"] == "warn" and res["n_unchecked"] == 1
    row = conn.execute(
        "SELECT integrity_status, gz_ok FROM files WHERE direction='R2'").fetchone()
    assert row["integrity_status"] == "unchecked" and row["gz_ok"] is None


def test_catalog_uses_stored_root(tmp_path):
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    # no seqdata_root passed -> falls back to projects.seqdata_root from ingest
    res = oint.check_catalog_integrity(conn)["genohub-1_X"]
    assert res["status"] == "pass" and res["n_ok"] == 2


def test_only_project_scopes(tmp_path):
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv",
                 [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")])
    make_project(root, "genohub-2_Y", "genohub-2_Y_mapfile.csv",
                 [("s2", "s2_1.fastq.gz", "s2_2.fastq.gz", "Salmo", "U2")])
    map_file = write_map_file(root, [
        ("genohub-1_X_mapfile.csv", "genohub-1_X"),
        ("genohub-2_Y_mapfile.csv", "genohub-2_Y")])
    conn = odb.connect(os.path.join(tmp_path, "cat.db"))
    odb.init_db(conn)
    oingest.ingest_map_file(conn, map_file, seqdata_root=root)
    res = oint.check_catalog_integrity(conn, seqdata_root=root, only_project="genohub-1_X")
    assert set(res) == {"genohub-1_X"}


def test_migration_adds_columns(tmp_path):
    """A pre-integrity DB gets the new files columns via _migrate (idempotent)."""
    conn = odb.connect(os.path.join(tmp_path, "old.db"))
    # Build a files table lacking the integrity columns.
    conn.executescript(
        "CREATE TABLE projects (project_id TEXT PRIMARY KEY);"
        "CREATE TABLE files (file_pk INTEGER PRIMARY KEY, project_id TEXT, filename TEXT);")
    conn.commit()
    odb._migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)")}
    assert {"integrity_status", "gz_ok", "n_reads", "integrity_date"} <= cols
    odb._migrate(conn)  # second run must not raise


# ---- batch emit-json checkpointing / resume --------------------------------

def _emit_pks(conn):
    return {r["direction"]: r["file_pk"]
            for r in conn.execute("SELECT direction, file_pk FROM files")}


def test_results_json_roundtrip_and_corrupt(tmp_path):
    out = os.path.join(tmp_path, "x.json")
    oint._write_results_json(out, "P1", "2020-01-01", {5: {"status": "ok", "n_reads": 10}})
    assert oint._load_results_json(out) == {5: {"status": "ok", "n_reads": 10, "cached": True}}
    # a job killed mid-write can leave a corrupt file -> start that project over
    with open(out, "w") as f:
        f.write("{ not valid json")
    assert oint._load_results_json(out) == {}
    assert oint._load_results_json(os.path.join(tmp_path, "missing.json")) == {}


def test_emit_json_resume_skips_seeded_files(tmp_path):
    # Both files are valid on disk. Pre-seed a JSON marking R1 with a (stale)
    # gzip_error, as if a prior job had checked it. Resume must trust the seed and
    # NOT re-read R1, while R2 (absent from the seed) is freshly checked -> ok.
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    pks = _emit_pks(conn)
    out = os.path.join(tmp_path, "genohub-1_X.json")
    with open(out, "w") as f:
        json.dump({"project_id": "genohub-1_X", "run_date": "2000-01-01",
                   "results": {str(pks["R1"]): {"status": "gzip_error", "n_reads": None}}}, f)

    payload = oint.emit_project_json(conn, "genohub-1_X", out, seqdata_root=root,
                                     progress=False)
    res = payload["results"]
    assert res[str(pks["R1"])]["status"] == "gzip_error"  # kept from seed, not re-read
    assert res[str(pks["R2"])]["status"] == "ok"          # freshly checked


def test_emit_json_recheck_ignores_seed(tmp_path):
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    conn, root = _setup(tmp_path, rows)
    pks = _emit_pks(conn)
    out = os.path.join(tmp_path, "genohub-1_X.json")
    with open(out, "w") as f:
        json.dump({"project_id": "genohub-1_X", "run_date": "2000-01-01",
                   "results": {str(pks["R1"]): {"status": "gzip_error", "n_reads": None}}}, f)

    # recheck re-reads every file, ignoring the seed -> R1 corrected to ok
    payload = oint.emit_project_json(conn, "genohub-1_X", out, seqdata_root=root,
                                     recheck=True, progress=False)
    assert payload["results"][str(pks["R1"])]["status"] == "ok"


def test_emit_json_checkpoints_during_run(tmp_path, monkeypatch):
    # With the checkpoint interval at 1, the JSON must exist (with progress) before
    # the run finishes -- proving results are flushed incrementally, not only at end.
    monkeypatch.setattr(oint, "_COMMIT_EVERY", 1)
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1"),
            ("s2", "s2_1.fastq.gz", "s2_2.fastq.gz", "Gadus", "U2")]
    conn, root = _setup(tmp_path, rows)
    out = os.path.join(tmp_path, "genohub-1_X.json")

    seen = {}

    real = oint._write_results_json

    def spy(path, pid, run_date, results):
        seen["max"] = max(seen.get("max", 0), len(results))
        seen["calls"] = seen.get("calls", 0) + 1
        return real(path, pid, run_date, results)

    monkeypatch.setattr(oint, "_write_results_json", spy)
    oint.emit_project_json(conn, "genohub-1_X", out, seqdata_root=root, progress=False)
    # 4 files -> at least one mid-run checkpoint + the final write (calls > 1)
    assert seen["calls"] > 1
    assert oint._load_results_json(out)  # a valid JSON exists at the end

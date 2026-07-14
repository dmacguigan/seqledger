"""Per-catalog config table + get/set/resolve helpers."""

import os
import sqlite3

from seqledger import db as odb


def _fresh(tmp_path):
    conn = odb.connect(os.path.join(tmp_path, "cat.db"))
    odb.init_db(conn)
    return conn


def test_defaults_when_unset(tmp_path):
    conn = _fresh(tmp_path)
    assert odb.get_config(conn, "conda_env") == "seqledger"
    assert odb.get_config(conn, "io_queue") == "lTIO.sq"
    assert odb.get_config(conn, "nope", "fallback") == "fallback"


def test_set_and_get_overrides_default(tmp_path):
    conn = _fresh(tmp_path)
    odb.set_config(conn, "conda_env", "otherlab")
    odb.set_config(conn, "catalog_name", "Deep Sea Catalog")
    conn.commit()
    assert odb.get_config(conn, "conda_env") == "otherlab"
    cfg = odb.resolve_config(conn)
    assert cfg["catalog_name"] == "Deep Sea Catalog"
    assert cfg["io_queue"] == "lTIO.sq"  # untouched -> default


def test_missing_config_table_falls_back(tmp_path):
    # An older read-only catalog copy predating the config table must not crash.
    p = os.path.join(tmp_path, "old.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE projects (project_id TEXT PRIMARY KEY)")
    con.commit()
    con.row_factory = sqlite3.Row
    assert odb.get_config(con, "conda_env") == "seqledger"
    assert odb.resolve_config(con)["io_queue"] == "lTIO.sq"


def test_fastq_globs():
    assert odb.fastq_globs("fastq.gz,fq.gz") == ["*.fastq.gz", "*.fq.gz"]
    assert odb.fastq_globs(".fastq.gz") == ["*.fastq.gz"]
    assert odb.fastq_globs("") == []


def test_ingest_uses_configured_roots(tmp_path, capsys):
    import gzip
    from seqledger import cli
    seq = str(tmp_path / "seq"); meta = str(tmp_path / "meta")
    os.makedirs(os.path.join(seq, "genohub-1_X")); os.makedirs(meta)
    for fn in ("s1_1.fastq.gz", "s1_2.fastq.gz"):
        with gzip.open(os.path.join(seq, "genohub-1_X", fn), "wb") as f:
            f.write(b"@r\nACGT\n+\nIIII\n")
    with open(os.path.join(meta, "genohub-1_X_mapfile.csv"), "w") as f:
        f.write("ID,R1,R2,Taxon,UniqID\ns1,s1_1.fastq.gz,s1_2.fastq.gz,Gadus,U1\n")
    db = str(tmp_path / "cat.db")
    # roots set only at init-db
    cli.main(["--db", db, "init-db", "--seqdata-root", seq, "--metadata-root", meta])
    # ingest with NO root flags -> must use the configured roots
    cli.main(["--db", db, "ingest", "--skip-taxonomy"])
    out = capsys.readouterr().out
    assert "using configured seqdata-root" in out
    assert "using configured metadata-root" in out
    conn = odb.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1

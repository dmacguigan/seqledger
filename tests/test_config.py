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

"""Per-catalog config table + get/set/resolve helpers."""

import os

from seqledger import db as odb


def test_defaults_when_unset(fresh_db):
    conn = fresh_db
    assert odb.get_config(conn, "conda_env") == "seqledger"
    assert odb.get_config(conn, "io_queue") == "lTIO.sq"
    assert odb.get_config(conn, "nope", "fallback") == "fallback"


def test_set_and_get_overrides_default(fresh_db):
    conn = fresh_db
    odb.set_config(conn, "conda_env", "otherlab")
    odb.set_config(conn, "catalog_name", "Deep Sea Catalog")
    conn.commit()
    assert odb.get_config(conn, "conda_env") == "otherlab"
    cfg = odb.resolve_config(conn)
    assert cfg["catalog_name"] == "Deep Sea Catalog"
    assert cfg["io_queue"] == "lTIO.sq"  # untouched -> default


def test_fresh_schema_loads_and_resolves_defaults(fresh_db):
    # Guard for #15: the new CHECK constraints in schema.sql must not break a fresh
    # init_db, and every config default still resolves on the resulting catalog.
    conn = fresh_db
    cfg = odb.resolve_config(conn)
    for key, default in odb.CONFIG_DEFAULTS.items():
        assert cfg[key] == default


def test_fastq_globs():
    assert odb.fastq_globs("fastq.gz,fq.gz") == ["*.fastq.gz", "*.fq.gz"]
    assert odb.fastq_globs(".fastq.gz") == ["*.fastq.gz"]
    assert odb.fastq_globs("") == ["*.fastq.gz", "*.fq.gz"]


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
    cli.main(["--db", db, "ingest", "--skip-taxonomy", "-v"])
    err = capsys.readouterr().err
    assert "using configured seqdata-root" in err
    assert "using configured metadata-root" in err
    conn = odb.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1

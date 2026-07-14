import importlib.util
import os

from seqledger import db as odb
from helpers import make_project, write_map_file
from test_taxonomy import _write_taxdump


def _load_cli():
    """Load seqledger.py (the CLI script) by path; `import seqledger` gets the package."""
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "seqledger.py")
    spec = importlib.util.spec_from_file_location("seqledger_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ingest_runs_taxonomy_and_datafiles_not_integrity(tmp_path, capsys):
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus morhua", "U1")]
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows)
    mf = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    db = str(tmp_path / "cat.db")

    cli = _load_cli()
    cli.main(["--db", db, "ingest", mf, "--seqdata-root", root, "--taxdir", taxdir])

    conn = odb.connect(db)
    # ingest happened
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
    # integrity is NOT part of ingest anymore -> not run, nothing persisted
    assert conn.execute(
        "SELECT integrity_status FROM files WHERE direction='R1'").fetchone()[0] is None
    # taxonomy resolved
    assert conn.execute(
        "SELECT taxid FROM taxa WHERE taxon='Gadus morhua'").fetchone()[0] == 8049
    # review CSV written next to the DB
    assert os.path.exists(os.path.join(os.path.dirname(db), "taxonomy_review.csv"))

    out = capsys.readouterr().out
    assert "== ingest ==" in out and "== integrity ==" not in out
    assert "== taxonomy resolve ==" in out and "ingest complete." in out
    assert "1 new sample(s)" in out
    # points the user at the separate integrity step
    assert "integrity" in out and "--batch" in out


def test_ingest_skip_taxonomy(tmp_path):
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus morhua", "U1")]
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows)
    mf = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])
    db = str(tmp_path / "cat.db")

    cli = _load_cli()
    cli.main(["--db", db, "ingest", mf, "--seqdata-root", root, "--skip-taxonomy"])

    conn = odb.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
    # taxonomy skipped, integrity never runs in ingest -> neither persisted
    assert conn.execute(
        "SELECT integrity_status FROM files WHERE direction='R1'").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM taxa").fetchone()[0] == 0


def test_ingest_without_seqdata_root_skips_datafiles(tmp_path, capsys):
    # No --seqdata-root: the data-files check is skipped with a note, but taxonomy
    # still runs.
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus morhua", "U1")]
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows)
    mf = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    db = str(tmp_path / "cat.db")

    cli = _load_cli()
    cli.main(["--db", db, "ingest", mf, "--taxdir", taxdir])

    out = capsys.readouterr().out
    assert "skipped: pass --seqdata-root" in out
    conn = odb.connect(db)
    assert conn.execute(
        "SELECT integrity_status FROM files WHERE direction='R1'").fetchone()[0] is None
    # taxonomy still resolved
    assert conn.execute(
        "SELECT taxid FROM taxa WHERE taxon='Gadus morhua'").fetchone()[0] == 8049


def test_reingest_unchanged_gates_taxonomy(tmp_path, capsys):
    # A second, identical ingest should not re-run taxonomy.
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus morhua", "U1")]
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows)
    mf = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    db = str(tmp_path / "cat.db")

    cli = _load_cli()
    cli.main(["--db", db, "ingest", mf, "--seqdata-root", root, "--taxdir", taxdir])
    capsys.readouterr()  # discard first run's output

    cli.main(["--db", db, "ingest", mf, "--seqdata-root", root, "--taxdir", taxdir])
    out = capsys.readouterr().out
    assert "0 new sample(s), 0 changed, 0 new file(s)" in out
    assert "(no new taxa to resolve)" in out

    conn = odb.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1


def test_reingest_changed_taxon_reresolves(tmp_path, capsys):
    # Editing a sample's Taxon updates the row and resolves the new name.
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    db = str(tmp_path / "cat.db")
    cli = _load_cli()

    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv",
                 [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus morhua", "U1")])
    mf = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])
    cli.main(["--db", db, "ingest", mf, "--seqdata-root", root, "--taxdir", taxdir])
    capsys.readouterr()

    # Rewrite the CSV with a corrected species name, re-ingest.
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv",
                 [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus macrocephalus", "U1")])
    cli.main(["--db", db, "ingest", mf, "--seqdata-root", root, "--taxdir", taxdir])
    out = capsys.readouterr().out
    assert "0 new sample(s), 1 changed" in out

    conn = odb.connect(db)
    assert conn.execute(
        "SELECT taxon FROM samples WHERE sample_id='s1'").fetchone()[0] == "Gadus macrocephalus"
    # the new name got resolved
    assert conn.execute(
        "SELECT taxid FROM taxa WHERE taxon='Gadus macrocephalus'").fetchone()[0] == 8050


def test_reingest_reports_dropped_sample_as_orphan(tmp_path, capsys):
    # A sample removed from the CSV is warned about but kept (not pruned).
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    db = str(tmp_path / "cat.db")
    cli = _load_cli()

    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", [
        ("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus morhua", "U1"),
        ("s2", "s2_1.fastq.gz", "s2_2.fastq.gz", "Gadus morhua", "U2"),
    ])
    mf = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])
    # No --seqdata-root: keep this focused on orphan detection, no disk warnings.
    cli.main(["--db", db, "ingest", mf, "--skip-taxonomy"])
    capsys.readouterr()

    # Drop s2 from the CSV, re-ingest.
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", [
        ("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus morhua", "U1"),
    ])
    cli.main(["--db", db, "ingest", mf, "--skip-taxonomy"])
    out = capsys.readouterr().out
    assert "sample s2 in catalog but not in this CSV" in out

    conn = odb.connect(db)
    # s2 is kept, not pruned
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 2


def test_ingest_prune_refreshes_data_files_report(tmp_path, capsys):
    # bad1's files are listed in the CSV but never created on disk -> flagged
    # 'missing from disk'. After removing bad1 and re-ingesting with --prune, the
    # pipeline's data-files check must clear those stale rows without a manual
    # `validate` (the bug: pruned file rows were gone but the report still showed
    # them missing).
    root = str(tmp_path / "raw_sequence_data")
    db = str(tmp_path / "cat.db")
    cli = _load_cli()

    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus morhua", "U1"),
            ("bad1", "bad1_1.fastq.gz", "bad1_2.fastq.gz", "Gadus morhua", "U2")]
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows,
                 disk_files=["s1_1.fastq.gz", "s1_2.fastq.gz"])
    mf = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])
    cli.main(["--db", db, "ingest", mf, "--seqdata-root", root, "--skip-taxonomy"])

    conn = odb.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM data_check_issues").fetchone()[0] == 2
    assert conn.execute(
        "SELECT data_check_n_missing FROM projects").fetchone()[0] == 2
    conn.close()

    # user drops bad1 from the CSV, re-ingests with --prune
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows[:1],
                 disk_files=["s1_1.fastq.gz", "s1_2.fastq.gz"])
    capsys.readouterr()
    cli.main(["--db", db, "ingest", mf, "--seqdata-root", root,
              "--skip-taxonomy", "--prune"])

    conn = odb.connect(db)
    # stale rows cleared and status recomputed clean, no manual validate needed
    assert conn.execute("SELECT COUNT(*) FROM data_check_issues").fetchone()[0] == 0
    assert conn.execute("SELECT data_check_n_missing FROM projects").fetchone()[0] == 0
    assert conn.execute("SELECT data_check_status FROM projects").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2

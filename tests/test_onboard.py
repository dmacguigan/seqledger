import importlib.util
import os

from odna import db as odb
from helpers import make_project, write_map_file
from test_taxonomy import _write_taxdump


def _load_cli():
    """Load odna.py (the CLI script) by path; `import odna` gets the package."""
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "odna.py")
    spec = importlib.util.spec_from_file_location("odna_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_onboard_runs_all_three(tmp_path, capsys):
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus morhua", "U1")]
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows)
    mf = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    db = str(tmp_path / "cat.db")

    cli = _load_cli()
    cli.main(["--db", db, "onboard", mf, "--seqdata-root", root, "--taxdir", taxdir])

    conn = odb.connect(db)
    # ingest happened
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
    # integrity ran and persisted (valid gzip fixtures -> ok)
    assert conn.execute(
        "SELECT integrity_status FROM files WHERE role='R1'").fetchone()[0] == "ok"
    assert conn.execute(
        "SELECT COUNT(*) FROM validation_log WHERE project_id='genohub-1_X'"
    ).fetchone()[0] == 1
    # taxonomy resolved
    assert conn.execute(
        "SELECT taxid FROM taxa WHERE taxon='Gadus morhua'").fetchone()[0] == 8049
    # review CSV written next to the DB
    assert os.path.exists(os.path.join(os.path.dirname(db), "taxonomy_review.csv"))

    out = capsys.readouterr().out
    assert "== ingest ==" in out and "== integrity ==" in out
    assert "== taxonomy resolve ==" in out and "onboard complete." in out


def test_onboard_skip_flags(tmp_path):
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus morhua", "U1")]
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows)
    mf = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])
    db = str(tmp_path / "cat.db")

    cli = _load_cli()
    cli.main(["--db", db, "onboard", mf, "--seqdata-root", root,
              "--skip-integrity", "--skip-taxonomy"])

    conn = odb.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
    # integrity + taxonomy skipped -> nothing persisted for them
    assert conn.execute(
        "SELECT integrity_status FROM files WHERE role='R1'").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM taxa").fetchone()[0] == 0

"""Menu shown by `seqledger` with no subcommand."""

import os
import subprocess
import sys

import pytest

from seqledger import cli, interactive
from seqledger import db as odb
from helpers import make_project

ROWS = [(f"s{i}", f"s{i}_1.fastq.gz", f"s{i}_2.fastq.gz", "Gadus morhua", f"U{i}")
        for i in (1, 2, 3)]


def _feed(monkeypatch, *answers):
    it = iter(answers)

    def fake(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    monkeypatch.setattr("builtins.input", fake)


def _catalog(tmp_path):
    root = str(tmp_path / "raw")
    make_project(root, "genohub-1_A", "genohub-1_A_mapfile.csv", ROWS)
    db = str(tmp_path / "cat.db")
    cli.main(["--db", db, "init-db", "--seqdata-root", root, "--metadata-root", root])
    cli.main(["--db", db, "ingest", "--skip-taxonomy"])
    return db, root


def _n_samples(db):
    conn = odb.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    conn.close()
    return n


def test_no_subcommand_off_terminal_is_usage_exit_2(tmp_path):
    src = os.path.dirname(os.path.dirname(interactive.__file__))
    r = subprocess.run([sys.executable, "-m", "seqledger"], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, cwd=tmp_path,
                       env=dict(os.environ, PYTHONPATH=src))
    assert r.returncode == 2 and r.stdout == ""
    assert "usage: seqledger" in r.stderr and "choose a command" in r.stderr


def test_no_subcommand_on_terminal_opens_menu(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(interactive, "run", lambda db: 7)
    assert cli.main(["--db", "nowhere.db"]) == 7


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_eof_and_ctrl_c_return_0(tmp_path, monkeypatch, exc):
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(exc))
    assert interactive.run(str(tmp_path / "cat.db")) == 0


def test_overview_echoes_command(tmp_path, monkeypatch, capsys):
    db, _ = _catalog(tmp_path)
    capsys.readouterr()
    _feed(monkeypatch, "2", "1", "")
    assert interactive.run(db) == 0
    out = capsys.readouterr().out
    assert f"seqledger --db {db} query summary" in out
    assert "genohub-1_A" in out and "(1 projects, 3 samples)" in out


def test_ingest_prune_needs_typed_delete(tmp_path, monkeypatch):
    db, root = _catalog(tmp_path)
    make_project(root, "genohub-1_A", "genohub-1_A_mapfile.csv", ROWS[:2])
    _feed(monkeypatch, "4", "", "", "n", "n", "")
    interactive.run(db)
    assert _n_samples(db) == 3
    _feed(monkeypatch, "4", "", "", "n", "DELETE", "")
    interactive.run(db)
    assert _n_samples(db) == 2


def test_no_taxdump_and_no_adds_skip_taxonomy(tmp_path, monkeypatch):
    db, _ = _catalog(tmp_path)
    seen = []
    monkeypatch.setattr(cli, "run", lambda argv: (seen.append(argv), (0, None))[1])
    _feed(monkeypatch, "4", "", "", "n", "")
    interactive.run(db)
    assert seen == [["--db", db, "ingest", "--skip-taxonomy"]]


def test_missing_catalog_and_no_creates_nothing(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "cat.db")
    other = str(tmp_path / "typo.db")
    _feed(monkeypatch, "1", "c", other, "n", "")
    assert interactive.run(db) == 0
    assert not os.path.exists(db) and not os.path.exists(other)
    out = capsys.readouterr().out
    assert "(not found)" in out and "ask your data manager" in out.lower()


def test_create_catalog_abspaths_roots(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _feed(monkeypatch, "c", "new.db", "y", "Lab", "raw", "meta", "")
    interactive.run("missing.db")
    conn = odb.connect(str(tmp_path / "new.db"))
    assert odb.get_config(conn, "seqdata_root") == str(tmp_path / "raw")
    assert f"export SEQLEDGER_DB={tmp_path / 'new.db'}" in capsys.readouterr().out


def test_menu_md5_jobs_toggle_and_show_all(tmp_path, monkeypatch, capsys):
    db, root = _catalog(tmp_path)
    make_project(root, "genohub-2_B", "genohub-2_B_mapfile.csv", ROWS)
    make_project(root, "genohub-3_C", "genohub-3_C_mapfile.csv", ROWS)
    cli.main(["--db", db, "ingest", "--skip-taxonomy"])
    conn = odb.connect(db)
    conn.execute("INSERT INTO backups (project_id, location, verified, n_files, n_mismatch) "
                 "VALUES ('genohub-1_A', 'pdrive', 1, 6, 0)")
    conn.commit()
    conn.close()
    capsys.readouterr()
    out_dir = str(tmp_path / "jobs")
    # 6 -> sub-menu 1 (make scripts); untick 2 (genohub-3_C); show all; tick 1 (genohub-1_A);
    # Enter = done; output dir; pdrive root; rclone exe
    _feed(monkeypatch, "6", "1", "2", "a", "1", "", out_dir, r"P:\raw", "", "")
    assert interactive.run(db) == 0
    out = capsys.readouterr().out
    assert "[x]  1  genohub-2_B" in out and "[x]  2  genohub-3_C" in out
    assert "[ ]  2  genohub-3_C" in out
    assert "[ ]  1  genohub-1_A" in out  # verified project appears unticked after 'a'
    assert "--project genohub-1_A --project genohub-2_B" in out
    assert "genohub-3_C" not in open(os.path.join(out_dir, "store_md5.sh")).read()


def test_menu_md5_jobs_default_runs_all_unverified(tmp_path, monkeypatch, capsys):
    db, root = _catalog(tmp_path)
    capsys.readouterr()
    out_dir = str(tmp_path / "jobs")
    _feed(monkeypatch, "6", "1", "", out_dir, "", "", "")
    assert interactive.run(db) == 0
    out = capsys.readouterr().out
    assert f"seqledger --db {db} md5-jobs --out {out_dir}" in out
    assert "--project" not in out
    assert os.path.exists(os.path.join(out_dir, "pdrive_md5.bat"))


def test_menu_checksums_load_still_works(tmp_path, monkeypatch, capsys):
    db, _ = _catalog(tmp_path)
    capsys.readouterr()
    _feed(monkeypatch, "6", "2", "/nonexistent", "/nonexistent", "")
    assert interactive.run(db) == 0
    assert "No file at" in capsys.readouterr().out

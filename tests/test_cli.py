"""CLI behavior: catalog-exists guard, --db position, exit codes, messages."""

import os
import shutil
import subprocess
import sys

import pytest

from seqledger import cli
from seqledger import db as odb
from helpers import make_project, write_map_file, write_md5

ROWS = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus morhua", "U1")]


def _run(argv):
    try:
        rc = cli.main(argv)
    except SystemExit as e:
        if not isinstance(e.code, int):
            print(e.code, file=sys.stderr)
        rc = e.code if isinstance(e.code, int) else 1
    return rc or 0


def _tree(tmp_path, names=("genohub-1_A",)):
    root = str(tmp_path / "raw")
    os.makedirs(root, exist_ok=True)
    for n in names:
        make_project(root, n, f"{n}_mapfile.csv", ROWS)
    return root


def _ingested(tmp_path, names=("genohub-1_A",)):
    root = _tree(tmp_path, names)
    db = str(tmp_path / "cat.db")
    _run(["--db", db, "init-db"])
    assert _run(["--db", db, "ingest", "--seqdata-root", root,
                 "--metadata-root", root, "--skip-taxonomy"]) == 0
    return db, root


@pytest.mark.parametrize("argv", [
    ["integrity"], ["taxonomy", "resolve"], ["checksums", "--store", "a", "--pdrive", "b"],
    ["ingest"], ["gui"], ["init-db", "--show"], ["query", "summary"], ["validate"],
])
def test_missing_catalog_is_refused_and_not_created(tmp_path, capsys, argv):
    db = str(tmp_path / "typo.db")
    assert _run(["--db", db, *argv]) == 1
    assert not os.path.exists(db)
    err = capsys.readouterr().err
    assert "no catalog at" in err and "SEQLEDGER_DB" in err
    assert "Data managers: create one with: seqledger --db PATH init-db" in err


def test_missing_catalog_without_db_flag_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SEQLEDGER_DB", raising=False)
    assert _run(["query", "summary"]) == 1
    assert "no --db given" in capsys.readouterr().err


def test_init_db_show_is_read_only(tmp_path, capsys):
    db = str(tmp_path / "cat.db")
    assert _run(["--db", db, "init-db"]) == 0
    capsys.readouterr()
    assert _run(["--db", db, "init-db", "--show", "--set", "foo=bar"]) == 0
    out = capsys.readouterr().out
    assert "io_queue" in out and "initialized" not in out
    conn = odb.connect_ro(db)
    assert odb.get_config(conn, "foo") is None
    conn.close()


def test_db_after_subcommand(tmp_path, capsys):
    db = str(tmp_path / "cat.db")
    assert _run(["init-db", "--db", db]) == 0
    assert os.path.exists(db)
    assert _run(["query", "summary", "--db", db]) == 0
    assert cli.build_parser().parse_args(
        ["taxonomy", "resolve", "--db", "x.db"]).db == "x.db"
    assert cli.build_parser().parse_args(
        ["--db", "y.db", "query", "summary"]).db == "y.db"


def test_ingest_fail_exits_1(tmp_path):
    root = str(tmp_path / "raw")
    os.makedirs(root)
    make_project(root, "p1", "p1_bad.csv", ROWS)
    mf = write_map_file(root, [("p1_bad.csv", "p1")])
    db = str(tmp_path / "cat.db")
    _run(["--db", db, "init-db"])
    assert _run(["--db", db, "ingest", mf, "--seqdata-root", root, "--skip-taxonomy"]) == 1


def test_ingest_zero_projects_not_complete(tmp_path, capsys):
    root = str(tmp_path / "empty")
    os.makedirs(root)
    db = str(tmp_path / "cat.db")
    _run(["--db", db, "init-db"])
    _run(["--db", db, "ingest", "--seqdata-root", root, "--metadata-root", root])
    assert "Next:" not in capsys.readouterr().out


def test_ingest_next_hint_omits_root_when_configured(tmp_path, capsys):
    root = _tree(tmp_path)
    db = str(tmp_path / "cat.db")
    _run(["--db", db, "init-db", "--seqdata-root", root, "--metadata-root", root])
    assert _run(["--db", db, "ingest", "--skip-taxonomy"]) == 0
    out = capsys.readouterr().out
    assert "Next: seqledger" in out and "<raw_sequence_data>" not in out


def test_ingest_offline_taxonomy_no_traceback(tmp_path, monkeypatch, capsys):
    from seqledger import taxonomy as otax

    def boom(*a, **k):
        raise RuntimeError("Failed to download NCBI taxdump")
    monkeypatch.setattr(otax, "resolve_catalog", boom)
    root = _tree(tmp_path)
    db = str(tmp_path / "cat.db")
    _run(["--db", db, "init-db"])
    assert _run(["--db", db, "ingest", "--seqdata-root", root, "--metadata-root", root]) == 0
    assert "taxonomy skipped" in capsys.readouterr().out


def test_integrity_gzip_error_exits_1(tmp_path):
    db, root = _ingested(tmp_path)
    with open(os.path.join(root, "genohub-1_A", "s1_1.fastq.gz"), "wb") as f:
        f.write(b"not gzip at all")
    assert _run(["--db", db, "integrity"]) == 1


def test_integrity_clean_exits_0(tmp_path):
    db, _ = _ingested(tmp_path)
    assert _run(["--db", db, "integrity"]) == 0


def test_validate_issues_exit_1(tmp_path, capsys):
    db, root = _ingested(tmp_path)
    capsys.readouterr()
    assert _run(["--db", db, "validate"]) == 0
    assert "skipped" not in capsys.readouterr().out
    os.remove(os.path.join(root, "genohub-1_A", "s1_2.fastq.gz"))
    assert _run(["--db", db, "validate"]) == 1


def test_checksums_mismatch_exits_1(tmp_path):
    db, _ = _ingested(tmp_path)
    s = write_md5(str(tmp_path / "s.md5"), [("a" * 32, "genohub-1_A/s1_1.fastq.gz"),
                                            ("b" * 32, "genohub-1_A/s1_2.fastq.gz")])
    p = write_md5(str(tmp_path / "p.md5"), [("a" * 32, "genohub-1_A/s1_1.fastq.gz"),
                                            ("c" * 32, "genohub-1_A/s1_2.fastq.gz")])
    assert _run(["--db", db, "checksums", "--store", s, "--pdrive", p]) == 1
    assert _run(["--db", db, "checksums", "--store", s, "--pdrive", s]) == 0


def test_prune_projects_refusal_prints_reason_exits_2(tmp_path, capsys):
    db, root = _ingested(tmp_path, ("p1", "p2", "p3"))
    for n in ("p1", "p2"):
        shutil.rmtree(os.path.join(root, n))
        os.remove(os.path.join(root, f"{n}_mapfile.csv"))
    capsys.readouterr()
    rc = _run(["--db", db, "ingest", "--seqdata-root", root, "--metadata-root", root,
               "--skip-taxonomy", "--prune-projects", "--yes"])
    assert rc == 2
    assert "would remove 2 of 3" in capsys.readouterr().out


def test_prune_guard_refusal_exits_2(tmp_path):
    db, root = _ingested(tmp_path)
    assert _run(["--db", db, "ingest", "--seqdata-root", root, "--metadata-root", root,
                 "--prune"]) == 2


def _fake_qsub(rc):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, rc, stdout="123\n", stderr="denied")
    return run


def test_batch_submit_shows_configured_queue(tmp_path, monkeypatch, capsys):
    db, _ = _ingested(tmp_path)
    _run(["--db", db, "init-db", "--io-queue", "myQ.q"])
    monkeypatch.setattr(cli.subprocess, "run", _fake_qsub(0))
    capsys.readouterr()
    assert _run(["--db", db, "integrity", "--batch",
                 "--batch-dir", str(tmp_path / "b")]) == 0
    out = capsys.readouterr().out
    assert "to myQ.q" in out and "lTIO.sq" not in out


def test_batch_qsub_failure_exits_2(tmp_path, monkeypatch):
    db, _ = _ingested(tmp_path)
    monkeypatch.setattr(cli.subprocess, "run", _fake_qsub(1))
    assert _run(["--db", db, "integrity", "--batch",
                 "--batch-dir", str(tmp_path / "b")]) == 2


@pytest.mark.parametrize("argv,n", [
    (["validate"], 0), (["-v", "validate"], 1), (["validate", "-vv"], 2),
    (["-vv", "ingest"], 2), (["taxonomy", "resolve", "-v"], 1)])
def test_verbose_flag_before_or_after_subcommand(argv, n):
    assert cli.build_parser().parse_args(argv).verbose == n


def test_ingest_default_is_summary_and_v_lists_projects(tmp_path, capsys):
    db, root = _ingested(tmp_path, ("genohub-1_A", "genohub-2_B"))
    out = capsys.readouterr().out
    assert "Ingest: 2 project(s) (1 OK, 1 with problems" in out  # shared UniqID
    assert "[OK]" not in out and "==" not in out and "genohub-1_A:" not in out
    _run(["--db", db, "ingest", "--seqdata-root", root, "--metadata-root", root,
          "--skip-taxonomy", "-v"])
    out = capsys.readouterr().out
    assert "nothing new or changed" in out
    assert "  genohub-1_A: " in out and "  genohub-2_B: " in out


def test_checksums_warnings_sorted_and_mismatch_named(tmp_path, capsys):
    db, _ = _ingested(tmp_path, ("genohub-1_A", "genohub-2_B"))
    files = [f"genohub-{n}/s1_{d}.fastq.gz" for n in ("2_B", "1_A") for d in (2, 1)]
    s = write_md5(str(tmp_path / "s.md5"), [("a" * 32, f) for f in files])
    p = write_md5(str(tmp_path / "p.md5"), [("a" * 32, files[0]), ("b" * 32, files[1])])
    capsys.readouterr()
    assert _run(["--db", db, "checksums", "--store", s, "--pdrive", p, "--force"]) == 1
    out = capsys.readouterr().out
    assert "genohub-2_B/s1_1.fastq.gz: md5 differs" in out
    assert "missing from P-drive" not in out
    _run(["--db", db, "-vv", "checksums", "--store", s, "--pdrive", p, "--force"])
    detail = [ln.strip() for ln in capsys.readouterr().out.splitlines()
              if ln.startswith("      ")]
    assert detail == sorted(detail) and len(detail) == 3


def test_query_empty_prints_none_on_stderr(tmp_path, capsys):
    db, _ = _ingested(tmp_path)
    capsys.readouterr()
    _run(["--db", db, "query", "mismatches"])
    out, err = capsys.readouterr()
    assert out == "" and "(none)" in err


def test_integrity_default_names_damaged_file(tmp_path, capsys):
    db, root = _ingested(tmp_path)
    with open(os.path.join(root, "genohub-1_A", "s1_1.fastq.gz"), "wb") as f:
        f.write(b"not gzip at all")
    capsys.readouterr()
    assert _run(["--db", db, "integrity"]) == 1
    out = capsys.readouterr().out
    assert "1 damaged file:" in out and "genohub-1_A/s1_1.fastq.gz: gzip error" in out
    assert "[OK]" not in out

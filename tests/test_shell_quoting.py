"""Shell/SGE injection guards for generated qsub and rclone scripts."""

import csv
import os
import shlex
import subprocess

import pytest

from seqledger import cli, gui
from seqledger import db as odb
from seqledger import ingest as oingest
from seqledger import rclone as orclone
from helpers import make_project


def test_set_config_rejects_shell_metachar_in_script_key(fresh_db):
    conn = fresh_db
    with pytest.raises(ValueError, match="conda_env"):
        odb.set_config(conn, "conda_env", "seqledger; touch A B #")


def test_set_config_rejects_newline_any_key(fresh_db):
    conn = fresh_db
    with pytest.raises(ValueError, match="io_queue"):
        odb.set_config(conn, "io_queue", "lTIO.sq\n#$ -M x@y -m bea")
    with pytest.raises(ValueError, match="newline"):
        odb.set_config(conn, "catalog_name", "a\nb")
    odb.set_config(conn, "catalog_name", "Deep Sea Catalog")  # spaces fine off-script


def test_init_db_set_rejects_bad_value(tmp_path):
    db = str(tmp_path / "cat.db")
    with pytest.raises(SystemExit):
        cli.main(["--db", db, "init-db", "--set", "conda_env=x;id"])
    conn = odb.connect(db)
    assert odb.get_config(conn, "conda_env") == "seqledger"


def test_batch_script_quotes_conda_env_and_rejects_queue_newline():
    s = cli._batch_script("p", "/l/p.log", 2, 4, "true",
                          conda_env="seqledger; touch A B #")
    assert "conda activate 'seqledger; touch A B #'" in s
    with pytest.raises(ValueError):
        cli._batch_script("p", "/l/p.log", 2, 4, "true",
                          io_queue="lTIO.sq\n#$ -M x@y")


def test_gui_job_script_quotes_conda_env_and_rejects_queue_newline():
    s = gui._gui_job_script("/l.log", "/r", "lTIO.sq", "env; id", 2, "/db", 8501)
    assert "conda activate 'env; id'" in s
    with pytest.raises(ValueError):
        gui._gui_job_script("/l.log", "/r", "q\n#$ -M x@y", "env", 2, "/db", 8501)


def test_tunnel_quotes_login_host(capsys):
    gui._print_tunnel("node1", 8501, "host; id")
    assert "'" in capsys.readouterr().out.split("ssh", 1)[1].splitlines()[0]


def test_copy_script_quotes_module_and_rejects_queue_newline():
    s = orclone.build_copy_script([("/r", ["a.fq.gz"])], "/d",
                                  rclone_module="tools/rclone; id")
    assert "module load 'tools/rclone; id'" in s
    with pytest.raises(ValueError):
        orclone.build_copy_script([("/r", ["a.fq.gz"])], "/d", io_queue="q\n#$ -M x@y")


def test_copy_script_rel_path_cannot_close_list(tmp_path):
    evil = "a_1.fastq.gz\nFILES\ntouch PWNED\ncat <<'FILES'\n"
    s = orclone.build_copy_script([("/r", [evil])], str(tmp_path / "dest"))
    assert shlex.quote(evil) in s
    subprocess.run(["bash", "-c", s], cwd=tmp_path, capture_output=True)
    assert not (tmp_path / "PWNED").exists()


def test_copy_script_quotes_destination_with_quote_and_space():
    s = orclone.build_copy_script([("/r", ["a.fq.gz"])], "/pool/my dir'; id")
    assert "DEST='/pool/my dir'\"'\"'; id'" in s


def test_ingest_skips_filename_with_newline(tmp_path, fresh_db):
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    mapfile, _ = make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv",
                              [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")])
    evil = "s1_1.fastq.gz\nFILES\ntouch PWNED\ncat <<'FILES'\n"
    with open(mapfile, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ID", "R1", "R2", "Taxon", "UniqID"])
        w.writerow(["s1", evil, "s1_2.fastq.gz", "Gadus", "U1"])
    conn = fresh_db
    findings = oingest.ingest_tree(conn, root, root)[0][1]
    rels = [r[0] for r in conn.execute("SELECT rel_path FROM files")]
    assert not any("\n" in r for r in rels), rels
    assert any("control character" in f.message for f in findings), \
        [str(f) for f in findings]

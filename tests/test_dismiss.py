import os, stat
import pytest
from seqledger import dismiss

def test_default_log_path_is_hidden_sibling(tmp_path):
    db = str(tmp_path / "oceandna_catalog.db")
    assert dismiss.default_log_path(db) == str(tmp_path / ".oceandna_catalog_dismissed.tsv")

def test_keys():
    assert dismiss.project_key("P1", "flagged", "3 of 40\trows\nflagged") == \
        ("project", "P1", "", "mapfile:flagged:3 of 40 rows flagged")
    assert dismiss.sample_key("P1", "s1", "na_uniqid;missing_r2") == \
        ("sample", "P1", "s1", "flags:na_uniqid;missing_r2")
    assert dismiss.file_key("P1", "P1/Run1/x.fq.gz", "md5 mismatch") == \
        ("file", "P1", "P1/Run1/x.fq.gz", "md5 mismatch")

def test_append_creates_664_with_header_and_read_round_trips(tmp_path):
    p = str(tmp_path / ".cat_dismissed.tsv")
    k = dismiss.file_key("P1", "P1/x.fq.gz", "gzip error")
    dismiss.append(p, "dismiss", k, "dan", "known\tbad\nfile")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o664
    lines = open(p).read().splitlines()
    assert lines[0] == dismiss.HEADER
    assert lines[1].split("\t")[1:] == ["dan", "dismiss", "file", "P1", "P1/x.fq.gz",
                                        "gzip error", "known bad file"]
    d, n_bad = dismiss.read(p)
    assert n_bad == 0 and set(d) == {k}
    assert d[k]["user"] == "dan" and d[k]["note"] == "known bad file"
    assert d[k]["time"].endswith("Z")

def test_last_action_wins_and_undo_removes(tmp_path):
    p = str(tmp_path / "log.tsv")
    k = dismiss.sample_key("P1", "s1", "na_uniqid")
    dismiss.append(p, "dismiss", k, "a")
    dismiss.append(p, "undo", k, "b")
    assert dismiss.read(p)[0] == {}
    dismiss.append(p, "dismiss", k, "c", "again")
    assert dismiss.read(p)[0][k]["user"] == "c"

def test_read_missing_file_and_bad_lines(tmp_path):
    assert dismiss.read(str(tmp_path / "none.tsv")) == ({}, 0)
    p = tmp_path / "log.tsv"
    p.write_text(dismiss.HEADER + "\r\n"
                 "2026-01-01T00:00:00Z\tu\tdismiss\tfile\tP1\tP1/a.gz\tgzip error\t\r\n"
                 "\n"
                 "garbage line\n"
                 "2026-01-01T00:00:01Z\tu\tdismiss\tfile\tP1\tP1/b.gz")  # partial line
    d, n_bad = dismiss.read(str(p))
    assert set(d) == {("file", "P1", "P1/a.gz", "gzip error")}
    assert n_bad == 2

def test_append_rejects_bad_action_and_propagates_oserror(tmp_path):
    with pytest.raises(ValueError):
        dismiss.append(str(tmp_path / "l.tsv"), "hide", dismiss.file_key("P", "f", "gzip error"), "u")
    with pytest.raises(OSError):
        dismiss.append(str(tmp_path / "missing_dir" / "l.tsv"),
                       "dismiss", dismiss.file_key("P", "f", "gzip error"), "u")

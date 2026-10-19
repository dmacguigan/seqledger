"""Tests for the read-only Streamlit GUI: pure helpers plus AppTest runs."""

import datetime
import math
import os
import sqlite3
import sys

import pandas as pd
import pytest

pytest.importorskip("streamlit")
from seqledger.app import streamlit_app as app  # noqa: E402

_APP_PATH = app.__file__


# --- #27 CSV formula injection -------------------------------------------------

def test_csv_safe_cell_prefixes_formula_leaders():
    assert app._csv_safe_cell("=SUM(A1)") == "'=SUM(A1)"
    assert app._csv_safe_cell("+1+1") == "'+1+1"
    assert app._csv_safe_cell("-2+3") == "'-2+3"
    assert app._csv_safe_cell("@cmd") == "'@cmd"
    assert app._csv_safe_cell("\ttab") == "'\ttab"
    assert app._csv_safe_cell("\rcr") == "'\rcr"


def test_csv_safe_cell_leaves_safe_values_untouched():
    assert app._csv_safe_cell("Gadus morhua") == "Gadus morhua"
    assert app._csv_safe_cell("") == ""
    assert app._csv_safe_cell(None) is None
    # numeric cells keep their type (a negative number is not a formula)
    assert app._csv_safe_cell(5) == 5
    assert app._csv_safe_cell(-5) == -5
    assert math.isnan(app._csv_safe_cell(float("nan")))


def test_csv_safe_df_only_touches_string_columns_and_does_not_mutate():
    df = pd.DataFrame({"taxon": ["=cmd()", "Gadus"], "n_reads": [1, 2]})
    out = app._csv_safe_df(df)
    assert list(out["taxon"]) == ["'=cmd()", "Gadus"]
    assert list(out["n_reads"]) == [1, 2]          # numeric column untouched
    assert list(df["taxon"]) == ["=cmd()", "Gadus"]  # original not mutated


def test_csv_safe_df_sanitizes_frame_from_read_sql_query():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (taxon TEXT, n INTEGER)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [("=cmd()", 1), ("Gadus", 2)])
    df = pd.read_sql_query("SELECT * FROM t", con)
    con.close()
    out = app._csv_safe_df(df)
    assert list(out["taxon"]) == ["'=cmd()", "Gadus"]
    assert list(out["n"]) == [1, 2]


# --- #26 regex ReDoS guard -----------------------------------------------------

def test_validate_regex_accepts_valid():
    ok, err = app._validate_regex("Gadus.*morhua")
    assert ok and err is None


def test_validate_regex_rejects_uncompilable():
    ok, err = app._validate_regex("(unbalanced")
    assert not ok and "invalid regex" in err


def test_validate_regex_rejects_overlong():
    ok, err = app._validate_regex("a" * (app._MAX_REGEX_LEN + 1))
    assert not ok and "too long" in err


# --- #9 database-locked handling ----------------------------------------------

def test_ro_connect_is_query_only(tmp_path):
    from seqledger import db as odb
    p = str(tmp_path / "a?b#c.db")
    odb.init_db(odb.connect(p))
    con = odb.connect_ro(p)
    assert con.execute("PRAGMA query_only").fetchone()[0] == 1
    assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
    with pytest.raises(sqlite3.OperationalError):
        con.execute("CREATE TABLE evil(x)")


# --- #25 empty files count as an integrity issue ------------------------------

def test_integrity_label_flags_empty_zero_read_files():
    base = {"n_files": 2, "n_integrity_ok": 2, "n_integrity_bad": 0,
            "n_integrity_empty": 0}
    assert app.integrity_label(dict(base)) == "verified"
    # an 'ok' file with 0 reads must NOT read as healthy
    assert app.integrity_label({**base, "n_integrity_empty": 1}) == "1 empty"


def test_integrity_label_reports_corrupt_and_empty_together():
    row = {"n_files": 3, "n_integrity_ok": 2, "n_integrity_bad": 1,
           "n_integrity_empty": 2}
    assert app.integrity_label(row) == "1 corrupt, 2 empty"


def test_integrity_label_other_states_unchanged():
    assert app.integrity_label(
        {"n_files": 0, "n_integrity_ok": 0, "n_integrity_bad": 0,
         "n_integrity_empty": 0}) == "no files"
    assert app.integrity_label(
        {"n_files": 2, "n_integrity_ok": 0, "n_integrity_bad": 0,
         "n_integrity_empty": 0}) == "unchecked"
    assert app.integrity_label(
        {"n_files": 3, "n_integrity_ok": 2, "n_integrity_bad": 0,
         "n_integrity_empty": 0}) == "incomplete (1 unchecked)"


# --- #28 charts count the same population -------------------------------------

def test_drop_unknown_filters_on_deepest_selected_rank():
    rank_cols = ["r1", "r2", "r3"]
    v = pd.DataFrame({
        "project_id": ["p1", "p1", "p1", "p1"],
        "r1": ["Euk", "Euk", "Euk", "Euk"],
        "r2": ["Animalia", "Animalia", "", None],
        "r3": ["Chordata", "Mollusca", "x", "y"],
    })
    # depth=2 -> filter on r2: '' and None normalize to 'unknown' and drop out
    out = app._drop_unknown(v, rank_cols, 2)
    assert list(out.index) == [0, 1]
    # depth=1 -> filter on r1 (all known) keeps everyone
    assert len(app._drop_unknown(v, rank_cols, 1)) == 4


# --- #30 per-column filters match the displayed value -------------------------

def test_link_text_extracts_label_after_hash():
    s = pd.Series(["https://x/123/#Gadus morhua",
                   "https://y/#Homo sapiens", None, "plain"])
    out = app._link_text(s)
    assert out.iloc[0] == "Gadus morhua"
    assert out.iloc[1] == "Homo sapiens"
    assert pd.isna(out.iloc[2])   # None -> blank
    assert pd.isna(out.iloc[3])   # no '#' -> no match


def test_filter_by_columns_uses_displayed_value_not_raw_url():
    df = pd.DataFrame({
        "name": ["Gadus morhua", "Homo sapiens", "Gadus ogac"],
        "url": ["https://a/#Gadus morhua", "https://b/#Homo sapiens",
                "https://c/#Gadus ogac"],
    })
    display = {"url": app._link_text(df["url"])}
    # filtering the DISPLAYED value 'gadus' keeps the two Gadus rows
    kept = app._filter_by_columns(df, {"url": "gadus"}, display)
    assert list(kept["name"]) == ["Gadus morhua", "Gadus ogac"]
    # 'https' is in every RAW url but in no displayed value -> displayed filter drops all
    assert len(app._filter_by_columns(df, {"url": "https"}, display)) == 0
    # ...whereas filtering the raw column would (wrongly) match everything
    assert len(app._filter_by_columns(df, {"url": "https"})) == 3


def test_filter_by_columns_preserves_all_columns():
    df = pd.DataFrame({"a": ["x1", "x2"], "b": ["keep", "drop"]})
    out = app._filter_by_columns(df, {"b": "keep"})
    assert list(out.columns) == ["a", "b"]
    assert list(out["a"]) == ["x1"]


# --- #39 catalog identity / staleness caption ---------------------------------

def test_catalog_caption_shows_abs_path_and_mtime():
    ts = datetime.datetime(2026, 7, 23, 10, 30).timestamp()
    cap = app._catalog_caption("cat.db", ts)
    assert os.path.abspath("cat.db") in cap
    assert "2026-07-23 10:30" in cap
    assert "last updated" in cap


# --- AppTest: selection, schema check, taxonomy guards -------------------------

def _make_catalog(tmp_path, samples=True):
    from seqledger import db as odb
    path = str(tmp_path / "catalog.db")
    con = odb.connect(path)
    odb.init_db(con)
    con.execute("INSERT INTO projects (project_id, description, metadata_status) "
                "VALUES ('P1', 'alpha', 'ok'), ('P2', 'beta', 'ok'), "
                "('P3', 'gamma', 'missing_mapfile')")
    if samples:
        rows = [("s1", "Gadus"), ("s2", "Homo"), ("s3", "Mus"), ("s4", "Gadus morhua")]
        for sid, tx in rows:
            cur = con.execute("INSERT INTO samples (project_id, sample_id, taxon) "
                              "VALUES ('P1', ?, ?)", (sid, tx))
            con.execute("INSERT INTO files (project_id, sample_pk, direction, filename, "
                        "rel_path) VALUES ('P1', ?, 'R1', ?, ?)",
                        (cur.lastrowid, f"{sid}_R1.fq.gz", f"P1/{sid}_R1.fq.gz"))
        con.execute("INSERT INTO taxa (taxon, match_type, worms_match_type) "
                    "VALUES ('Gadus', 'fuzzy_species', 'review_phonetic')")
    con.commit()
    con.close()
    return path


def _apptest(monkeypatch, db_path, view):
    testing = pytest.importorskip("streamlit.testing.v1")
    monkeypatch.setenv("SEQLEDGER_DB", db_path)
    at = testing.AppTest.from_file(_APP_PATH, default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value(view).run()
    return at


def _table_key(at, prefix):
    return next(k for k in at.session_state.filtered_state if str(k).startswith(prefix))


_SEARCH = {"Samples": ("samples_table_", "gadus"),
           "Projects": ("projects_table_", "alpha"),
           "Files": ("files_table_", "s1_")}


@pytest.mark.parametrize("view", list(_SEARCH))
def test_stale_selection_after_filter_does_not_crash(tmp_path, monkeypatch, view):
    prefix, query = _SEARCH[view]
    at = _apptest(monkeypatch, _make_catalog(tmp_path), view)
    at.session_state[_table_key(at, prefix)] = {"selection": {"rows": [2], "columns": []}}
    at.run()
    assert not at.exception
    at.sidebar.text_input[0].set_value(query).run()
    assert not at.exception
    # Even if a stale out-of-range row reaches the filtered table, it is ignored.
    at.session_state[_table_key(at, prefix)] = {"selection": {"rows": [2], "columns": []}}
    at.run()
    assert not at.exception


def test_samples_selection_resets_when_filter_changes(tmp_path, monkeypatch):
    at = _apptest(monkeypatch, _make_catalog(tmp_path), "Samples")
    at.session_state[_table_key(at, "samples_table_")] = {
        "selection": {"rows": [2], "columns": []}}
    at.run()
    assert any("s3" in h.value for h in at.subheader)
    at.sidebar.text_input[0].set_value("gadus").run()
    assert not at.exception
    assert not any("Files for sample" in h.value for h in at.subheader)


def test_samples_table_shows_match_labels(tmp_path, monkeypatch):
    at = _apptest(monkeypatch, _make_catalog(tmp_path), "Samples")
    shown = at.dataframe[0].value
    assert "fuzzy (species)" in set(shown["tax_match"])
    assert "review: phonetic (sounds-like)" in set(shown["worms_match"])


def test_old_catalog_copy_shows_schema_error(tmp_path, monkeypatch):
    path = _make_catalog(tmp_path)
    con = sqlite3.connect(path)
    con.execute("DROP TABLE data_check_issues")
    con.commit()
    con.close()
    assert app._missing_schema(path) == ["data_check_issues"]
    testing = pytest.importorskip("streamlit.testing.v1")
    monkeypatch.setenv("SEQLEDGER_DB", path)
    at = testing.AppTest.from_file(_APP_PATH, default_timeout=30).run()
    assert not at.exception
    assert "missing: data_check_issues" in at.error[0].value


def test_missing_schema_empty_on_current_catalog(tmp_path):
    assert app._missing_schema(_make_catalog(tmp_path)) == []


def test_taxonomy_without_plotly_shows_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "plotly.express", None)
    at = _apptest(monkeypatch, _make_catalog(tmp_path), "Taxonomy")
    assert not at.exception
    assert "needs plotly" in at.error[0].value


def test_taxonomy_empty_catalog_message(tmp_path, monkeypatch):
    pytest.importorskip("plotly")
    at = _apptest(monkeypatch, _make_catalog(tmp_path, samples=False), "Taxonomy")
    assert not at.exception
    assert any("No samples in this catalog yet." in i.value for i in at.info)


def test_match_dicts_consistent():
    for order, labels, colors in [
            (app.MATCH_ORDER, app.MATCH_LABELS, app.MATCH_COLORS),
            (app.WORMS_MATCH_ORDER, app.WORMS_MATCH_LABELS, app.WORMS_MATCH_COLORS)]:
        assert set(order) == set(labels) == set(colors)
    assert "ambiguous" in app.MATCH_ORDER
    assert "review_phonetic" in app.WORMS_MATCH_ORDER


# --- Grab & Go: problem files, lane splits, map paths ------------------------

def test_file_problem_and_files_ok():
    fp = app._file_problem
    assert fp({"integrity_status": "ok", "n_reads": 10, "missing_on_disk": 0}) == "ok"
    assert fp({"integrity_status": "ok", "n_reads": 0, "missing_on_disk": 0}) == "empty"
    assert fp({"integrity_status": "gzip_error", "n_reads": None}) == "corrupt"
    assert fp({"integrity_status": "unchecked", "n_reads": float("nan")}) == "unchecked"
    assert fp({"integrity_status": "ok", "n_reads": 5, "missing_on_disk": 1}) == "missing"
    assert app._files_ok(["ok", "ok"]) == "ok"
    assert app._files_ok(["corrupt", "missing", "corrupt", "unchecked"]) == \
        "2 corrupt, 1 missing, unchecked"
    assert app._files_ok([]) == "no files"


def _grab_catalog(tmp_path):
    from seqledger import db as odb
    p = str(tmp_path / "cat.db")
    con = odb.connect(p)
    odb.init_db(con)
    con.execute("INSERT INTO projects(project_id, seqdata_root) VALUES ('P1', '/store/root')")
    for pk, sid in ((1, "s1"), (2, "s2")):
        con.execute("INSERT INTO samples(sample_pk, project_id, sample_id, taxon) "
                    "VALUES (?, 'P1', ?, 'Gadus morhua')", (pk, sid))
    files = [  # (sample_pk, direction, rel_path, integrity_status, n_reads)
        (1, "R1", "P1/L2/s1_R1.fastq.gz", "ok", 5),
        (1, "R1", "P1/L1/s1_R1.fastq.gz", "ok", 5),
        (1, "R2", "P1/L2/s1_R2.fastq.gz", "ok", 5),
        (1, "R2", "P1/L1/s1_R2.fastq.gz", "ok", 5),
        (2, "R1", "P1/s2_R1.fastq.gz", "gzip_error", None),
        (2, "R2", "P1/s2_R2.fastq.gz", "ok", 5),
    ]
    for pk, d, rel, st_, n in files:
        con.execute("INSERT INTO files(project_id, sample_pk, direction, filename, rel_path, "
                    "size_bytes, integrity_status, n_reads) VALUES ('P1',?,?,?,?,100,?,?)",
                    (pk, d, os.path.basename(rel), rel, st_, n))
    con.execute("INSERT INTO data_check_issues(project_id, kind, filename) "
                "VALUES ('P1', 'missing from disk', 'P1/s2_R2.fastq.gz')")
    con.commit()
    con.close()
    return p


def test_load_samples_lists_every_lane_file(tmp_path):
    p = _grab_catalog(tmp_path)
    df = app.load_samples(p, os.path.getmtime(p))
    s1 = df[df["sample_id"] == "s1"].iloc[0]
    assert s1["r1_path"] == ("/store/root/P1/L1/s1_R1.fastq.gz; "
                             "/store/root/P1/L2/s1_R1.fastq.gz")


def test_grab_and_go_flags_problem_files_and_lane_splits(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    p = _grab_catalog(tmp_path)
    monkeypatch.setenv("SEQLEDGER_DB", p)
    at = AppTest.from_file(_APP_PATH, default_timeout=30)
    at.session_state["cart"] = {
        f"P1\t{s}": {"project_id": "P1", "sample_id": s} for s in ("s1", "s2")}
    at.run()
    at.sidebar.radio[0].set_value("Grab & Go").run()
    warnings = " ".join(w.value for w in at.warning)
    assert "2 of 6 files in the cart are corrupt, empty, or missing on disk" in warnings
    assert "1 sample(s) in the cart have more than one read pair" in warnings and "(s1)" in warnings
    assert at.checkbox[0].value is True
    dest = next(t for t in at.text_input if t.label.startswith("Destination"))
    dest.set_value("/pool/dest").run()
    assert any("Script preview (4 files total" in e.label for e in at.expander)
    script = at.code[0].value
    assert "s2_R1.fastq.gz" not in script and "s2_R2.fastq.gz" not in script
    # unticking the exclude box puts the problem files back in the job
    at.checkbox[0].uncheck().run()
    assert any("Script preview (6 files total" in e.label for e in at.expander)

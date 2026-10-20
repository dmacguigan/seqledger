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


def test_grab_and_go_includes_files_with_dismissed_problems(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from seqledger import dismiss
    p = _grab_catalog(tmp_path)
    log = dismiss.default_log_path(p)
    dismiss.append(log, "dismiss", dismiss.file_key("P1", "P1/s2_R1.fastq.gz", "gzip error"), "u")
    dismiss.append(log, "dismiss", dismiss.file_key("P1", "P1/s2_R2.fastq.gz", "missing from disk"), "u")
    monkeypatch.setenv("SEQLEDGER_DB", p)
    at = AppTest.from_file(_APP_PATH, default_timeout=30)
    at.session_state["cart"] = {f"P1\t{s}": {"project_id": "P1", "sample_id": s} for s in ("s1", "s2")}
    at.run()
    at.sidebar.radio[0].set_value("Grab & Go").run()
    table = at.dataframe[1].value
    assert table[table["sample_id"] == "s2"].iloc[0]["files_ok"] == "ok (+2 dismissed)"
    assert not any("files in the cart are corrupt" in w.value for w in at.warning)
    dest = next(t for t in at.text_input if t.label.startswith("Destination"))
    dest.set_value("/pool/dest").run()
    assert any("Script preview (6 files total" in e.label for e in at.expander)


def _issues_df(rows):
    cols = ["project_id", "kind", "filename"]
    return pd.DataFrame(rows, columns=cols).assign(full_path="", sample_id="", direction="",
                                                   taxon="", uniq_id="", uniq_id_url="")

def _prow(**kw):
    base = dict(project_id="P1", metadata_status="ok", metadata_detail="", data_check_status="issues",
                data_check_n_missing=0, data_check_n_orphan=0, n_files=4, n_integrity_ok=4,
                n_integrity_bad=0, n_integrity_empty=0, n_mismatch=0, n_uncompared=0)
    base.update(kw); return pd.Series(base)

def test_project_status_rolls_up_live_and_dismissed():
    from seqledger import dismiss
    issues = _issues_df([("P1", "missing from mapfile", "P1/Run1/a.gz"),
                         ("P1", "missing from mapfile", "P1/Run2/a.gz"),
                         ("P1", "missing from disk", "P1/b.gz"),
                         ("P1", "md5 mismatch", "P1/b.gz"),
                         ("P1", "gzip error", "P1/c.gz")])
    prow = _prow(data_check_n_missing=1, data_check_n_orphan=2, n_integrity_bad=1,
                 n_integrity_ok=3, n_mismatch=1)
    none = app._mark_issues(issues, {})
    s = app._project_status(prow, none, {})
    assert s["data_files"] == "1 file(s) in mapfile not on disk, 2 file(s) on disk, not in mapfile"
    assert s["integrity"] == "1 corrupt" and s["checksum"] == "1 file(s) differ between Store and P-drive"
    d = {dismiss.file_key("P1", "P1/Run1/a.gz", "missing from mapfile"): {"user": "u", "time": "t", "note": ""},
         dismiss.file_key("P1", "P1/b.gz", "missing from disk"): {"user": "u", "time": "t", "note": ""},
         dismiss.file_key("P1", "P1/c.gz", "gzip error"): {"user": "u", "time": "t", "note": ""}}
    marked = app._mark_issues(issues, d)
    assert list(marked["dismissed"]) == [True, False, True, False, True]
    s = app._project_status(prow, marked, d)
    assert s["data_files"] == "1 file(s) on disk, not in mapfile (+2 dismissed)"
    assert s["integrity"] == "verified (+1 dismissed)"
    assert s["checksum"] == "1 file(s) differ between Store and P-drive"   # md5 key untouched

def test_project_status_mapfile_and_incomplete_variants():
    from seqledger import dismiss
    prow = _prow(metadata_status="flagged", metadata_detail="3 rows\tflagged", n_integrity_ok=2,
                 n_integrity_bad=1, n_uncompared=2, n_mismatch=1)
    issues = app._mark_issues(_issues_df([("P1", "gzip error", "P1/c.gz"),
                                          ("P1", "md5 mismatch", "P1/c.gz")]), {})
    s = app._project_status(prow, issues, {})
    assert s["mapfile"] == "flagged rows"
    d = {dismiss.project_key("P1", "flagged", "3 rows\tflagged"): {"user": "u", "time": "t", "note": ""},
         dismiss.file_key("P1", "P1/c.gz", "gzip error"): {"user": "u", "time": "t", "note": ""},
         dismiss.file_key("P1", "P1/c.gz", "md5 mismatch"): {"user": "u", "time": "t", "note": ""}}
    issues = app._mark_issues(issues, d)
    s = app._project_status(prow, issues, d)
    assert s["mapfile"] == "OK (+1 dismissed)"
    assert s["integrity"] == "incomplete (1 unchecked) (+1 dismissed)"
    assert s["checksum"] == "backup not yet verified (2 file(s)) (+1 dismissed)"

def test_severity_and_tint():
    sv = app._severity
    assert sv("gzip error") == "error" and sv("1 corrupt") == "error" and sv("Mismatch") == "error"
    assert sv("missing from mapfile") == "warning" and sv("na_uniqid") == "warning"
    assert sv("unchecked") == "pending" and sv("incomplete (2 unchecked)") == "pending"
    assert sv("OK (+1 dismissed)") == "dismissed" and sv("OK") == "ok" and sv("") == "ok" and sv("Verified") == "ok"
    assert sv("1 corrupt (+1 dismissed)") == "error"
    assert sv("ok (+2 dismissed)") == "dismissed"
    assert sv("unchecked (data folder not found: /x/empty_runs)") == "pending"
    assert sv("verified (+2 dismissed)") == "dismissed" and sv("flagged rows (dismissed)") == "dismissed"
    styler = app._tint(pd.DataFrame({"integrity": ["gzip error", "ok"], "x": [1, 2]}), ["integrity", "nope"])
    html = styler.to_html()
    assert "rgba(213, 94, 0, 0.30)" in html

def test_file_problem_honours_dismissals():
    from seqledger import dismiss
    r = {"project_id": "P1", "rel_path": "P1/x.gz", "integrity_status": "gzip_error", "n_reads": None}
    assert app._file_problem(r) == "corrupt"
    d = {dismiss.file_key("P1", "P1/x.gz", "gzip error"): {}}
    assert app._file_problem(r, d) == "dismissed"
    m = {"project_id": "P1", "rel_path": "P1/y.gz", "integrity_status": "ok", "n_reads": 5, "missing_on_disk": 1}
    assert app._file_problem(m, {dismiss.file_key("P1", "P1/y.gz", "missing from disk"): {}}) == "dismissed"
    assert app._files_ok(["ok", "dismissed"]) == "ok (+1 dismissed)"
    assert app._files_ok(["corrupt", "dismissed"]) == "1 corrupt (+1 dismissed)"

def test_md5_issues_loader(tmp_path):
    p = _grab_catalog(tmp_path)
    con = sqlite3.connect(p)
    con.execute("UPDATE files SET md5_match=0 WHERE rel_path='P1/s2_R2.fastq.gz'")
    con.commit(); con.close()
    df = app.load_md5_issues(p, os.path.getmtime(p))
    assert list(df["kind"]) == ["md5 mismatch"] and list(df["filename"]) == ["P1/s2_R2.fastq.gz"]
    integ = app.load_integrity_issues(p, os.path.getmtime(p))
    assert list(integ["filename"]) == ["P1/s2_R1.fastq.gz"]   # rel_path, not basename


def _issues_catalog(tmp_path):
    p = _grab_catalog(tmp_path)
    con = sqlite3.connect(p)
    con.execute("UPDATE projects SET data_check_status='issues', data_check_n_missing=1, "
                "metadata_status='flagged', metadata_detail='1 row flagged' WHERE project_id='P1'")
    con.execute("UPDATE files SET md5_match=(rel_path != 'P1/s2_R2.fastq.gz')")
    con.commit(); con.close()
    return p

def _select(at, key, rows):
    # A browser keeps table selections across reruns; AppTest drops them, so re-apply.
    from streamlit.proto.WidgetStates_pb2 import WidgetStates
    from streamlit.testing.v1.element_tree import get_widget_state
    if not hasattr(at, "_sel"):
        at._sel, run = {}, at._run

        def _run(*a, **kw):
            for k, r in at._sel.items():
                at.session_state[k] = {"selection": {"rows": r, "columns": []}}
            return run(*a, **kw)
        at._run = _run
    at._sel[key] = rows
    # AppTest keeps widgets that an st.rerun() removed; skip them.
    ws = WidgetStates()
    for node in at._tree:
        try:
            w = get_widget_state(node)
        except KeyError:
            continue
        if w is not None:
            ws.widgets.append(w)
    return at._run(ws)

def _button(at, label):
    return next(b for b in at.button if b.label.startswith(label))

def test_projects_dismiss_all_then_show_and_undo(tmp_path, monkeypatch):
    from seqledger import dismiss
    p = _issues_catalog(tmp_path)
    at = _apptest(monkeypatch, p, "Projects")
    _select(at, _table_key(at, "projects_table_"), [0])
    assert at.dataframe[0].value.iloc[0]["data_files"] == "1 file(s) in mapfile not on disk"
    assert any("1 row flagged" in w.value for w in at.warning)
    issues = at.dataframe[1].value
    assert sorted(issues["issue"]) == ["gzip error", "md5 mismatch", "missing from disk"]
    at.text_input(key="note_issues_P1").set_value("bulk")
    _button(at, "Yes, dismiss all").click().run()
    _select(at, _table_key(at, "projects_table_"), [0])
    row = at.dataframe[0].value.iloc[0]
    assert row["data_files"] == "OK (+1 dismissed)"
    assert row["integrity"] == "verified (+1 dismissed)"
    assert row["checksum"] == "backup verified (+1 dismissed)"
    assert "Data-files issues" in " ".join(h.value for h in at.subheader)
    assert len(at.dataframe) == 1   # no live rows: table replaced by a caption
    assert any("3 dismissed issue(s) hidden" in c.value for c in at.caption)
    log, _ = dismiss.read(dismiss.default_log_path(p))
    assert len(log) == 3 and all(v["note"] == "bulk" for v in log.values())
    # mapfile dismiss
    at.text_input(key="note_mapfile_P1").set_value("**bold** [x](y)")
    _button(at, "Dismiss mapfile issue").click().run()
    _select(at, _table_key(at, "projects_table_"), [0])
    assert at.dataframe[0].value.iloc[0]["mapfile"] == "OK (+1 dismissed)"
    assert not at.warning
    assert any("mapfile issue dismissed by" in m.value
               and "\\*\\*bold\\*\\* \\[x\\]\\(y\\)" in m.value for m in at.markdown)
    assert any(b.label == "Restore mapfile issue" for b in at.button)
    # show dismissed -> rows back with who/when, undo one
    at.sidebar.checkbox[-1].check().run()
    _select(at, _table_key(at, "projects_table_"), [0])
    issues = at.dataframe[1].value
    assert len(issues) == 3 and set(issues["dismissed_by"]) != {""}
    assert all(i.endswith(" (dismissed)") for i in issues["issue"])
    assert at.dataframe[0].value.iloc[0]["mapfile"] == "OK (+1 dismissed)"
    at.sidebar.checkbox[0].check().run()   # Only mapfile issues: tests live labels
    assert len(at.dataframe[0].value) == 0
    at.sidebar.checkbox[0].uncheck().run()
    _select(at, _table_key(at, "projects_table_"), [0])
    _select(at, _table_key(at, "issues_table_P1"), [0])
    _button(at, "Restore 1 ticked").click().run()
    assert len(dismiss.read(dismiss.default_log_path(p))[0]) == 3   # 4 dismissed, 1 undone

def test_projects_dismiss_unwritable_log_shows_hint(tmp_path, monkeypatch):
    p = _issues_catalog(tmp_path)
    log = tmp_path / ".cat_dismissed.tsv"
    log.write_text("")
    log.chmod(0o444)
    if os.access(str(log), os.W_OK):
        pytest.skip("running as root")
    at = _apptest(monkeypatch, p, "Projects")
    _select(at, _table_key(at, "projects_table_"), [0])
    _button(at, "Yes, dismiss all").click().run()
    assert not at.exception
    assert any("chmod 664" in e.value for e in at.error)

def test_projects_sidebar_reports_unreadable_lines(tmp_path, monkeypatch):
    p = _issues_catalog(tmp_path)
    (tmp_path / ".cat_dismissed.tsv").write_text("junk\n")
    at = _apptest(monkeypatch, p, "Projects")
    assert any("1 unreadable line(s)" in c.value for c in at.sidebar.caption)

def _capture_downloads(monkeypatch):
    import streamlit as st
    got = {}
    monkeypatch.setattr(st, "download_button",
                        lambda label, data, name, *a, **kw: got.__setitem__(name, data))
    return got

def _download_frame(got, suffix):
    import io
    return pd.read_csv(io.BytesIO(next(v for k, v in got.items() if k.endswith(suffix))))

def test_samples_dismiss_flags(tmp_path, monkeypatch):
    from seqledger import dismiss
    p = _issues_catalog(tmp_path)
    con = sqlite3.connect(p); con.execute("UPDATE samples SET flags='na_uniqid' WHERE sample_id='s1'")
    con.commit(); con.close()
    got = _capture_downloads(monkeypatch)
    at = _apptest(monkeypatch, p, "Samples")
    key = _table_key(at, "samples_table_")
    i = list(at.dataframe[0].value["sample_id"]).index("s1")
    _select(at, key, [i])
    _button(at, "Dismiss flags").click().run()
    assert at.dataframe[0].value.iloc[i]["flags"] == "OK (+1 dismissed)"
    assert any(b.label == "Restore flags" for b in at.button)
    assert dismiss.sample_key("P1", "s1", "na_uniqid") in dismiss.read(dismiss.default_log_path(p))[0]
    csv = _download_frame(got, "_samples.csv")
    assert csv.loc[csv["sample_id"] == "s1", "flags"].iloc[0] == "na_uniqid"
    at.sidebar.checkbox[-1].check().run()
    assert at.dataframe[0].value.iloc[i]["flags"] == "OK (+1 dismissed)"
    at.sidebar.radio[0].set_value("Grab & Go").run()
    at.sidebar.radio[0].set_value("Samples").run()
    assert at.sidebar.checkbox[-1].value is True

def test_files_dismiss_integrity_and_md5(tmp_path, monkeypatch):
    from seqledger import dismiss
    p = _issues_catalog(tmp_path)
    got = _capture_downloads(monkeypatch)
    at = _apptest(monkeypatch, p, "Files")
    key = _table_key(at, "files_table_")
    df = at.dataframe[0].value
    i = list(df["filename"]).index("s2_R1.fastq.gz")   # gzip_error
    _select(at, key, [i])
    at.text_input(key="note_file_P1_P1/s2_R1.fastq.gz").set_value("for R1 only")
    _button(at, "Dismiss gzip error").click().run()
    assert at.dataframe[0].value.iloc[i]["integrity"] == "OK (+1 dismissed)"
    j = list(at.dataframe[0].value["filename"]).index("s2_R2.fastq.gz")   # md5 mismatch
    _select(at, key, [j])
    assert at.text_input(key="note_file_P1_P1/s2_R2.fastq.gz").value == ""   # note did not follow
    _button(at, "Dismiss md5 mismatch").click().run()
    assert at.dataframe[0].value.iloc[j]["backup"] == "OK (+1 dismissed)"
    log, _ = dismiss.read(dismiss.default_log_path(p))
    assert log[dismiss.file_key("P1", "P1/s2_R1.fastq.gz", "gzip error")]["note"] == "for R1 only"
    assert log[dismiss.file_key("P1", "P1/s2_R2.fastq.gz", "md5 mismatch")]["note"] == ""
    csv = _download_frame(got, "_files.csv").set_index("filename")
    assert csv.loc["s2_R1.fastq.gz", "integrity"] == "gzip_error"
    assert csv.loc["s2_R2.fastq.gz", "backup"] == "Mismatch"
    # the integrity filter no longer returns the dismissed file
    sel = next(s for s in at.sidebar.selectbox if s.label == "Integrity")
    sel.set_value("gzip_error").run()
    assert "s2_R1.fastq.gz" not in list(at.dataframe[0].value["filename"])

def _labels():
    dl, cl, il, fo = (app.ovalidate.data_label, app.ovalidate.checksum_label,
                      app.integrity_label, app._files_ok)
    ints = {"n_files": 3, "n_integrity_ok": 3, "n_integrity_bad": 0, "n_integrity_empty": 0}
    cs = {"n_files": 3, "n_mismatch": 0, "n_uncompared": 0}
    return [
        (dl({"status": "ok"}), "ok"),
        (dl({"status": "unchecked"}), "pending"),
        (dl({"status": "unchecked", "detail": "data folder not found: /x"}), "pending"),
        (dl({"status": "issues", "n_missing": 1, "n_orphan": 0}), "error"),
        (dl({"status": "issues", "n_missing": 0, "n_orphan": 2}), "warning"),
        (dl({"status": "issues", "n_missing": 1, "n_orphan": 2}), "error"),
        (cl({**cs, "n_files": 0}), "pending"),
        (cl({**cs, "n_mismatch": 1}), "error"),
        (cl({**cs, "n_uncompared": 2}), "pending"),
        (cl(cs), "ok"),
        (il({**ints, "n_files": 0}), "pending"),
        (il({**ints, "n_integrity_ok": 2, "n_integrity_bad": 1}), "error"),
        (il({**ints, "n_integrity_empty": 1}), "error"),
        (il({**ints, "n_integrity_ok": 0}), "pending"),
        (il({**ints, "n_integrity_ok": 2}), "pending"),
        (il(ints), "ok"),
    ] + [(app._MAPFILE_LABEL[k], v) for k, v in (
        ("ok", "ok"), ("missing_mapfile", "error"), ("missing_seqdata", "error"),
        ("broken_mapfile", "error"), ("invalid_mapfile", "error"), ("flagged", "warning"))
    ] + [
        (fo([]), "pending"), (fo(["ok"]), "ok"), (fo(["ok", "dismissed"]), "dismissed"),
        (fo(["missing"]), "error"), (fo(["missing", "unchecked"]), "error"),
        (fo(["missing", "dismissed"]), "error"), (fo(["empty"]), "error"),
        (fo(["corrupt", "dismissed"]), "error"), (fo(["unchecked"]), "pending"),
        ("Verified", "ok"), ("Mismatch", "error"), ("Not compared", "pending"),
        ("ok", "ok"), ("gzip_error", "error"), ("format_error", "error"), ("unchecked", "pending"),
        ("missing from disk", "error"), ("missing from mapfile", "warning"),
        ("md5 mismatch", "error"), ("empty (0 reads)", "error"),
        ("gzip error (dismissed)", "dismissed"), ("missing from mapfile (dismissed)", "dismissed"),
    ]

@pytest.mark.parametrize("label,severity", _labels())
def test_severity_of_real_labels(label, severity):
    assert app._severity(label) == severity

def test_tint_skips_styler_on_large_frames():
    big = pd.DataFrame("x", index=range(10001), columns=[f"c{i}" for i in range(11)])
    assert isinstance(app._tint(big, ["c0"]), pd.DataFrame)
    assert not isinstance(app._tint(pd.DataFrame({"c0": ["x"]}), ["c0"]), pd.DataFrame)

def test_projects_unavailable_log_hides_dismiss(tmp_path, monkeypatch):
    p = _issues_catalog(tmp_path)
    con = sqlite3.connect(p)
    con.execute("INSERT OR REPLACE INTO config(key, value) VALUES ('dismiss_log', ?)",
                (str(tmp_path / "nope" / ".x.tsv"),))
    con.commit(); con.close()
    at = _apptest(monkeypatch, p, "Projects")
    _select(at, _table_key(at, "projects_table_"), [0])
    assert not at.exception
    assert any("Dismissals unavailable" in c.value for c in at.sidebar.caption)
    assert len(at.dataframe) == 2
    assert not any(b.label.startswith(("Dismiss", "Restore", "Yes, dismiss")) for b in at.button)

def test_projects_only_checksum_keeps_uncompared(tmp_path, monkeypatch):
    at = _apptest(monkeypatch, _make_catalog(tmp_path), "Projects")
    next(c for c in at.sidebar.checkbox if c.label == "Only checksum issues").check().run()
    assert list(at.dataframe[0].value["project_id"]) == ["P1"]

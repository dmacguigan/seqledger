"""Unit tests for the PURE helpers in the read-only Streamlit GUI.

Importing app/streamlit_app.py pulls in `streamlit` (for its @st.cache_data
decorators) even though these tests never touch the Streamlit runtime. When
streamlit isn't installed we inject a minimal stand-in so the module imports; the
functions under test (`_csv_safe_*`, `_validate_regex`, `_with_retry`, `_drop_unknown`,
`_link_text`, `_filter_by_columns`, `_catalog_caption`, `integrity_label`) never call
`st.*`, so a real vs. faked streamlit makes no difference to what is verified.
"""

import datetime
import importlib.util
import math
import os
import sqlite3
import sys
import types

import pandas as pd
import pytest

# app/ has no __init__.py (streamlit runs it as a bare script), so load it by path.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_PATH = os.path.join(_REPO_ROOT, "seqledger", "app", "streamlit_app.py")


def _load_app_module():
    try:
        import streamlit  # noqa: F401  (real streamlit if the GUI env is installed)
    except Exception:
        fake = types.ModuleType("streamlit")

        def _cache_data(*a, **k):
            def deco(fn):
                return fn
            return deco

        fake.cache_data = _cache_data
        fake.column_config = types.SimpleNamespace(LinkColumn=lambda *a, **k: None)
        sys.modules["streamlit"] = fake
    sys.path.insert(0, _REPO_ROOT)
    spec = importlib.util.spec_from_file_location("seqledger_streamlit_app", _APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


app = _load_app_module()


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

def test_is_locked_error_discriminates():
    assert app._is_locked_error(sqlite3.OperationalError("database is locked"))
    assert app._is_locked_error(sqlite3.OperationalError("database table is busy"))
    assert not app._is_locked_error(sqlite3.OperationalError("no such column: x"))
    assert not app._is_locked_error(ValueError("locked"))  # wrong exception type


def test_with_retry_returns_after_transient_lock(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *a: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise sqlite3.OperationalError("database is locked")
        return "value"

    assert app._with_retry(flaky) == "value"
    assert calls["n"] == 2


def test_with_retry_raises_catalog_locked_when_persistent(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *a: None)

    def always_locked():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(app._CatalogLocked):
        app._with_retry(always_locked)


def test_with_retry_propagates_non_lock_errors(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *a: None)

    def real_error():
        raise sqlite3.OperationalError("no such table: files")

    with pytest.raises(sqlite3.OperationalError):
        app._with_retry(real_error)


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

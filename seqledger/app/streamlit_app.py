"""Read-only Streamlit GUI for browsing the Ocean DNA sequence data catalog.

Launch via `seqledger gui --db PATH` (which sets SEQLEDGER_DB and prints the SSH
tunnel command). No SQL knowledge required: pick a view, search, filter, download CSV.

Views:
  Projects     one row per sequencing project, with summary stats + owner
  Samples      one row per sample (CSV export carries full R1/R2 paths + owner)
  Files        one row per FASTQ, full absolute path, size, owner, backup status
  Taxonomy     interactive breadth of sample taxonomy (NCBI or WoRMS)
  Grab & Go    search + collect samples; export CSV + an rclone copy job for them
"""

import datetime
import getpass
import os
import re
import sqlite3
import sys

import pandas as pd
import streamlit as st

# Reach the seqledger package (this app lives in seqledger/app/); add the package
# parent so `import seqledger` works when run as a bare script (not pip-installed).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from seqledger import rclone as orclone  # noqa: E402
from seqledger import mitopilot as omito  # noqa: E402
from seqledger import db as odb  # noqa: E402
from seqledger import dismiss  # noqa: E402
from seqledger import validate as ovalidate  # noqa: E402

NCBI_TAX_URL = "https://www.ncbi.nlm.nih.gov/datasets/taxonomy/"
RANK_COLS = ["tax_domain", "tax_kingdom", "tax_phylum", "tax_class",
             "tax_order", "tax_family", "tax_genus", "tax_species"]
RANK_LABELS = ["domain", "kingdom", "phylum", "class",
               "order", "family", "genus", "species"]

# taxa.match_type ordered best -> worst, with an accessible good->uncertain color
# ramp (works on light + dark). Samples with no taxa row fall into 'unresolved'.
MATCH_ORDER = ["confirmed", "exact", "fuzzy_species", "fuzzy_genus",
               "fuzzy_higher", "ambiguous", "unresolved"]
MATCH_LABELS = {"confirmed": "confirmed", "exact": "exact",
                "fuzzy_species": "fuzzy (species)", "fuzzy_genus": "fuzzy (genus)",
                "fuzzy_higher": "fuzzy (higher)", "ambiguous": "ambiguous (homonym)",
                "unresolved": "unresolved"}
MATCH_COLORS = {"confirmed": "#0B7268", "exact": "#3C8A57",
                "fuzzy_species": "#C9A227", "fuzzy_genus": "#C77D11",
                "fuzzy_higher": "#B5532A", "ambiguous": "#8E6BB0",
                "unresolved": "#7A8A86"}

# WoRMS (World Register of Marine Species) resolution, populated by
# `taxonomy resolve --source worms`. WoRMS's top rank is Kingdom (no domain), so its
# lineage is one level shorter than the NCBI one.
WORMS_TAX_URL = "https://www.marinespecies.org/aphia.php?p=taxdetails&id="
WORMS_RANK_COLS = ["worms_kingdom", "worms_phylum", "worms_class", "worms_order",
                   "worms_family", "worms_genus", "worms_species"]
WORMS_RANK_LABELS = ["kingdom", "phylum", "class", "order",
                     "family", "genus", "species"]
# WoRMS match types as stored by worms.py, best -> worst. Loose TAXAMATCH matches
# are stored with a 'review_' prefix and left unaccepted.
WORMS_MATCH_ORDER = ["confirmed", "exact", "near_1", "review_phonetic", "review_near_2",
                     "review_near_3", "review_match_quarantine", "unresolved"]
WORMS_MATCH_LABELS = {"confirmed": "confirmed", "exact": "exact",
                      "near_1": "near (1 edit)",
                      "review_phonetic": "review: phonetic (sounds-like)",
                      "review_near_2": "review: near (2 edits)",
                      "review_near_3": "review: near (3 edits)",
                      "review_match_quarantine": "review: quarantined",
                      "unresolved": "unresolved"}
WORMS_MATCH_COLORS = {"confirmed": "#0B7268", "exact": "#3C8A57", "near_1": "#C9A227",
                      "review_phonetic": "#C77D11", "review_near_2": "#C77D11",
                      "review_near_3": "#B5532A", "review_match_quarantine": "#B5532A",
                      "unresolved": "#7A8A86"}

# Registry the Taxonomy view switches between with its source toggle. Each entry is
# self-contained so the chart/quality/composition helpers are source-agnostic.
SOURCES = {
    "NCBI": {"rank_cols": RANK_COLS, "rank_labels": RANK_LABELS,
             "match_col": "match_type", "match_order": MATCH_ORDER,
             "match_labels": MATCH_LABELS, "match_colors": MATCH_COLORS,
             "blurb": "against NCBI"},
    "WoRMS": {"rank_cols": WORMS_RANK_COLS, "rank_labels": WORMS_RANK_LABELS,
              "match_col": "worms_match_type", "match_order": WORMS_MATCH_ORDER,
              "match_labels": WORMS_MATCH_LABELS, "match_colors": WORMS_MATCH_COLORS,
              "blurb": "against WoRMS (World Register of Marine Species)"},
}

DB_PATH = os.environ.get("SEQLEDGER_DB", "catalog.db")

# Full absolute path when the seqdata_root was captured at ingest, else the relpath.
_FULL_PATH = "COALESCE(p.seqdata_root || '/' || f.rel_path, f.rel_path)"

# data_check_issues.kind -> (short label, one-line explanation).
_ISSUE_LABEL = {
    "missing from disk":    "missing from disk",
    "missing from mapfile": "missing from mapfile",
    # Integrity-check failures (files.integrity_status / n_reads), surfaced in the
    # same per-project issues table as the disk/mapfile checks.
    "gzip error":           "gzip error",
    "format error":         "format error",
    "empty (0 reads)":      "empty (0 reads)",
    "md5 mismatch":         "md5 mismatch",
}
_ISSUE_DETAIL = {
    "missing from disk":    "Sequence file is listed in the mapfile but was not found on disk.",
    "missing from mapfile": "Sequence file is present on disk but is not referenced by any mapfile row.",
    "gzip error":           "File is not valid gzip or is truncated (failed decompression / `gunzip -t`).",
    "format error":         "File decompresses but is not valid FASTQ (line count not a multiple of 4).",
    "empty (0 reads)":      "File passed the gzip/FASTQ check but contains 0 reads (empty file or failed transfer?).",
    "md5 mismatch":         "Store and backup md5 differ for this file (one copy changed or is corrupt).",
}

# projects.metadata_status -> (short label, plain-english meaning). The full
# per-project sentence is stored in metadata_detail; this is the at-a-glance label.
_MAPFILE_LABEL = {
    "ok":              "OK",
    "missing_mapfile": "no mapfile",
    "missing_seqdata": "no data folder",
    "broken_mapfile":  "broken mapfile",
    "invalid_mapfile": "no usable rows",
    "flagged":         "flagged rows",
}
# Tables -> columns the GUI queries; an older catalog copy missing any is refused.
_REQUIRED_SCHEMA = {
    "projects": ["project_id", "source", "description", "seq_data_relpath",
                 "seqdata_root", "owner_name", "date_ingested", "metadata_status",
                 "metadata_detail", "data_check_status", "data_check_n_missing",
                 "data_check_n_orphan", "data_check_date"],
    "samples": ["sample_pk", "project_id", "sample_id", "taxon", "uniq_id", "flags"],
    "files": ["project_id", "sample_pk", "direction", "filename", "rel_path",
              "size_bytes", "owner_name", "md5_match", "integrity_status", "n_reads",
              "integrity_date"],
    "taxa": ["taxon", "match_type", "taxid", "sci_name", "lineage", "aphia_id",
             "worms_sci_name", "worms_status", "worms_match_type", "worms_lineage"]
            + RANK_COLS + WORMS_RANK_COLS,
    "data_check_issues": ["project_id", "kind", "filename"],
    "backups": ["project_id", "location", "verified"],
    "config": ["key", "value"],
}


def _missing_schema(db_path):
    """List of required tables / table.columns absent from the catalog."""
    con = odb.connect_ro(db_path)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = []
        for table, cols in _REQUIRED_SCHEMA.items():
            if table not in tables:
                missing.append(table)
                continue
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            missing += [f"{table}.{c}" for c in cols if c not in have]
        return missing
    finally:
        con.close()


def _sql(db_path, query):
    con = odb.connect_ro(db_path)
    try:
        return pd.read_sql_query(query, con)
    finally:
        con.close()


def _config(db_path, key):
    """Per-catalog config value (falls back to seqledger defaults)."""
    try:
        con = odb.connect_ro(db_path)
        try:
            return odb.get_config(con, key)
        finally:
            con.close()
    except sqlite3.Error:
        return odb.CONFIG_DEFAULTS.get(key)


def _dismiss_log_path(db_path):
    return _config(db_path, "dismiss_log") or dismiss.default_log_path(db_path)


@st.cache_data(ttl=60)
def load_dismissals(path, mtime, size):
    return dismiss.read(path)


_UNAVAILABLE = "Dismissals unavailable"


def _dismiss_state():
    path = _dismiss_log_path(DB_PATH)
    unavailable = f"{_UNAVAILABLE}: cannot read {path}"
    if not os.path.isdir(os.path.dirname(os.path.abspath(path))):
        return path, {}, unavailable
    try:
        s = os.stat(path) if os.path.exists(path) else None
        dismissed, n_bad = load_dismissals(path, s and s.st_mtime, s and s.st_size)
    except OSError:
        return path, {}, unavailable
    return path, dismissed, (f"{n_bad} unreadable line(s) in {path}" if n_bad else None)


def _dismiss_sidebar():
    """(dismissed, show_dismissed, available, log_path)"""
    path, dismissed, problem = _dismiss_state()
    show = st.checkbox("Show dismissed", key="show_dismissed",
                       value=st.session_state.get("show_dismissed_value", False))
    st.session_state["show_dismissed_value"] = show
    if problem:
        st.caption(problem)
    return dismissed, show, not (problem or "").startswith(_UNAVAILABLE), path


def _md(text):
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|<>~$:])", r"\\\1", str(text))


def _record(log_path, action, keys, note):
    try:
        dismiss.append_many(log_path, action, keys, getpass.getuser(), note)
    except OSError:
        st.error(f"Could not record the dismissal: {log_path} is not writable. "
                 f"Ask your data manager to run: chmod 664 {log_path}")
        return False
    load_dismissals.clear()
    return True


# A UniqID is sometimes a URL (e.g. a specimen record page). Render those as a
# clickable link in a companion column while keeping the plain text column intact.
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def _as_url(v):
    """Return the value if it's an http(s) URL, else None (blank in a LinkColumn)."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s if _URL_RE.match(s) else None


def _with_uniqid_url(df):
    """Add a `uniq_id_url` column (the URL when uniq_id is one, else None)."""
    if "uniq_id" in df.columns:
        df = df.copy()
        df["uniq_id_url"] = df["uniq_id"].map(_as_url)
    return df


# column_config entry to render uniq_id_url as a compact clickable link.
def _uniqid_link_col():
    return st.column_config.LinkColumn("uniq_id_link", display_text="open ↗")


@st.cache_data(ttl=60)
def load_samples(db_path, mtime):
    # worms_url embeds the accepted name in a URL '#'-fragment so the link column can
    # display the name as its label (a LinkColumn display_text regex), exactly like
    # ncbi_url -- no separate name/link columns. worms_name is kept for search + CSV.
    worms_sel = (
        "t.worms_sci_name AS worms_name, t.worms_status AS worms_status, "
        "t.worms_match_type AS worms_match, t.aphia_id AS aphia_id, "
        "t.worms_lineage AS worms_lineage, "
        f"CASE WHEN t.aphia_id IS NOT NULL THEN '{WORMS_TAX_URL}' || t.aphia_id "
        "|| '#' || COALESCE(t.worms_sci_name, s.taxon) END AS worms_url")
    return _with_uniqid_url(_sql(db_path, f"""
        SELECT s.project_id, s.sample_id, s.taxon, s.uniq_id, s.flags,
               p.source, p.seq_data_relpath AS data_dir,
               COALESCE(b.verified, 0) AS backup_verified,
               t.sci_name AS tax_name, t.match_type AS tax_match,
               t.taxid AS taxid, t.lineage AS lineage,
               CASE WHEN t.taxid IS NOT NULL
                    THEN '{NCBI_TAX_URL}' || t.taxid || '/#'
                         || COALESCE(t.sci_name, s.taxon) END AS ncbi_url,
               {worms_sel},
               (SELECT GROUP_CONCAT(fp, '; ') FROM (
                  SELECT {_FULL_PATH} AS fp FROM files f
                  WHERE f.project_id = s.project_id AND f.sample_pk = s.sample_pk
                    AND f.direction = 'R1' ORDER BY f.rel_path)) AS r1_path,
               (SELECT f.owner_name FROM files f
                  WHERE f.project_id = s.project_id AND f.sample_pk = s.sample_pk
                    AND f.direction = 'R1') AS r1_owner,
               (SELECT GROUP_CONCAT(fp, '; ') FROM (
                  SELECT {_FULL_PATH} AS fp FROM files f
                  WHERE f.project_id = s.project_id AND f.sample_pk = s.sample_pk
                    AND f.direction = 'R2' ORDER BY f.rel_path)) AS r2_path,
               (SELECT f.owner_name FROM files f
                  WHERE f.project_id = s.project_id AND f.sample_pk = s.sample_pk
                    AND f.direction = 'R2') AS r2_owner
        FROM samples s
        JOIN projects p ON p.project_id = s.project_id
        LEFT JOIN backups b ON b.project_id = s.project_id AND b.location = '{_config(db_path, 'backup_location').replace("'", "''")}'
        LEFT JOIN taxa t ON t.taxon = s.taxon
        ORDER BY s.project_id, s.sample_id"""))


@st.cache_data(ttl=60)
def load_taxonomy(db_path, mtime):
    # Select both the NCBI and WoRMS rank/match columns so the view's source toggle
    # can switch client-side.
    extra = ", ".join(f"t.{c}" for c in RANK_COLS + ["worms_match_type"] + WORMS_RANK_COLS)
    return _sql(db_path, f"""
        SELECT s.project_id, s.sample_id, s.taxon, t.taxid, t.match_type,
               {extra}
        FROM samples s
        LEFT JOIN taxa t ON t.taxon = s.taxon
        ORDER BY s.project_id, s.sample_id""")


@st.cache_data(ttl=60)
def load_projects(db_path, mtime):
    return _sql(db_path, """
        SELECT p.project_id, p.source, p.description,
               (SELECT COUNT(*) FROM samples s WHERE s.project_id = p.project_id) AS n_samples,
               (SELECT COUNT(*) FROM files f WHERE f.project_id = p.project_id) AS n_files,
               COALESCE(p.data_check_status, 'unchecked') AS data_check_status,
               p.data_check_n_missing, p.data_check_n_orphan,
               (SELECT COUNT(*) FROM files f
                  WHERE f.project_id = p.project_id AND f.md5_match = 0) AS n_mismatch,
               (SELECT COUNT(*) FROM files f
                  WHERE f.project_id = p.project_id AND f.md5_match IS NULL) AS n_uncompared,
               (SELECT COUNT(*) FROM files f
                  WHERE f.project_id = p.project_id AND f.integrity_status = 'ok') AS n_integrity_ok,
               (SELECT COUNT(*) FROM files f
                  WHERE f.project_id = p.project_id
                    AND f.integrity_status IN ('gzip_error', 'format_error')) AS n_integrity_bad,
               (SELECT COUNT(*) FROM files f
                  WHERE f.project_id = p.project_id
                    AND f.integrity_status = 'ok' AND f.n_reads = 0) AS n_integrity_empty,
               (SELECT MAX(f.integrity_date) FROM files f
                  WHERE f.project_id = p.project_id) AS integrity_date,
               p.owner_name, p.seq_data_relpath AS data_dir,
               p.metadata_status,
               p.metadata_detail,
               p.data_check_date, p.date_ingested
        FROM projects p
        ORDER BY p.project_id""")


@st.cache_data(ttl=60)
def load_data_issues(db_path, mtime):
    return _with_uniqid_url(_sql(db_path, f"""
        SELECT i.project_id, i.kind, i.filename,
               {_FULL_PATH} AS full_path,
               s.sample_id, f.direction, s.taxon, s.uniq_id
        -- i.filename holds the file's rel_path (physical identity); join on rel_path.
        FROM data_check_issues i
        LEFT JOIN files f ON f.project_id = i.project_id AND f.rel_path = i.filename
        LEFT JOIN projects p ON p.project_id = i.project_id
        LEFT JOIN samples s ON s.sample_pk = f.sample_pk
        ORDER BY i.project_id, i.kind, i.filename"""))


@st.cache_data(ttl=60)
def load_integrity_issues(db_path, mtime):
    """Files that failed the integrity check (corrupt or empty), shaped exactly like
    load_data_issues so both feed the one per-project 'Data-files issues' table.

    'gzip_error'/'format_error' are hard failures; a status of 'ok' with n_reads == 0
    is an empty file that passed the gzip/FASTQ check but carries no data. Unchecked
    files are not listed -- 'not yet verified' is a project-level state, not a per-file
    problem (the project row's integrity column already reports the unchecked count).
    """
    return _with_uniqid_url(_sql(db_path, f"""
        SELECT f.project_id,
               CASE f.integrity_status
                    WHEN 'gzip_error' THEN 'gzip error'
                    WHEN 'format_error' THEN 'format error'
                    ELSE 'empty (0 reads)' END AS kind,
               f.rel_path AS filename, {_FULL_PATH} AS full_path,
               s.sample_id, f.direction, s.taxon, s.uniq_id
        FROM files f
        LEFT JOIN projects p ON p.project_id = f.project_id
        LEFT JOIN samples s ON s.sample_pk = f.sample_pk
        WHERE f.integrity_status IN ('gzip_error', 'format_error')
           OR (f.integrity_status = 'ok' AND f.n_reads = 0)
        ORDER BY f.project_id, kind, f.rel_path"""))


@st.cache_data(ttl=60)
def load_md5_issues(db_path, mtime):
    return _with_uniqid_url(_sql(db_path, f"""
        SELECT f.project_id, 'md5 mismatch' AS kind,
               f.rel_path AS filename, {_FULL_PATH} AS full_path,
               s.sample_id, f.direction, s.taxon, s.uniq_id
        FROM files f
        LEFT JOIN projects p ON p.project_id = f.project_id
        LEFT JOIN samples s ON s.sample_pk = f.sample_pk
        WHERE f.md5_match = 0
        ORDER BY f.project_id, f.rel_path"""))


def _issue_key(row):
    return dismiss.file_key(row["project_id"], row["filename"], row["kind"])


def _mark_issues(issues, dismissed):
    out = issues.copy()
    hits = [dismissed.get(_issue_key(r)) for r in out.to_dict("records")]
    out["dismissed"] = pd.Series([h is not None for h in hits], index=out.index, dtype=bool)
    for col, field in (("dismissed_by", "user"), ("dismissed_on", "time"), ("note", "note")):
        out[col] = [(h or {}).get(field, "") for h in hits]
    return out


def backup_label(v):
    """Plain-english P-drive backup state from the 1/0 backups.verified flag."""
    return "Verified" if v == 1 else "Not verified"


def integrity_label(row):
    n = row["n_files"]
    if n == 0:
        return "no files"
    ok = int(row["n_integrity_ok"] or 0)
    bad = int(row["n_integrity_bad"] or 0)
    # Files with status 'ok' but 0 reads passed the gzip/FASTQ check yet carry no
    # data; they count within `ok` but are a real problem, so flag them here instead
    # of letting them read as healthy (they're also listed in the drill-down).
    empty = int(row.get("n_integrity_empty") or 0)
    problems = []
    if bad:
        problems.append(f"{bad} corrupt")
    if empty:
        problems.append(f"{empty} empty")
    if problems:
        return ", ".join(problems)
    if ok == 0:
        return "unchecked"
    rest = n - ok - bad
    if rest:
        return f"incomplete ({rest} unchecked)"
    return "verified"


def _rollup(live_label, full_label, n_dis, has_live, clear_label, show_dismissed):
    if not n_dis:
        return full_label
    if show_dismissed:
        return f"{full_label} ({n_dis} dismissed)"
    return live_label + " *" if has_live else clear_label


def _project_status(prow, issues, dismissed, show_dismissed):
    status = prow["metadata_status"]
    mapfile = _MAPFILE_LABEL.get(status, status)
    if isinstance(status, str) and \
            dismiss.project_key(prow["project_id"], status, prow["metadata_detail"]) in dismissed:
        mapfile = mapfile + " (dismissed)" if show_dismissed else "OK *"

    live = issues.loc[~issues["dismissed"], "kind"].value_counts()
    dis = issues.loc[issues["dismissed"], "kind"].value_counts()

    def n(counts, *kinds):
        return int(sum(counts.get(k, 0) for k in kinds))

    data = {"status": prow["data_check_status"], "n_missing": prow["data_check_n_missing"],
            "n_orphan": prow["data_check_n_orphan"]}
    data_full = ovalidate.data_label(data)
    if prow["data_check_status"] == "issues":
        lm, lo = n(live, "missing from disk"), n(live, "missing from mapfile")
        data_files = _rollup(ovalidate.data_label({**data, "n_missing": lm, "n_orphan": lo}),
                             data_full, n(dis, "missing from disk", "missing from mapfile"),
                             lm + lo > 0, "OK *", show_dismissed)
    else:
        data_files = data_full

    lb, le = n(live, "gzip error", "format error"), n(live, "empty (0 reads)")
    rest = (int(prow["n_files"] or 0) - int(prow["n_integrity_ok"] or 0)
            - int(prow["n_integrity_bad"] or 0))
    integrity = _rollup(
        integrity_label({**prow, "n_integrity_bad": lb, "n_integrity_empty": le}),
        integrity_label(prow), n(dis, "gzip error", "format error", "empty (0 reads)"),
        lb + le > 0, "OK *" if rest == 0 else f"incomplete ({rest} unchecked) *", show_dismissed)

    lmm, n_unc = n(live, "md5 mismatch"), int(prow["n_uncompared"] or 0)
    cs = {"n_files": prow["n_files"], "n_mismatch": prow["n_mismatch"], "n_uncompared": n_unc}
    checksum = _rollup(
        ovalidate.checksum_label({**cs, "n_mismatch": lmm}), ovalidate.checksum_label(cs),
        n(dis, "md5 mismatch"), lmm > 0,
        "OK *" if n_unc == 0 else f"incomplete ({n_unc} not compared) *", show_dismissed)

    return {"mapfile": mapfile, "data_files": data_files, "checksum": checksum,
            "integrity": integrity}


_ERROR_LABELS = {"no mapfile", "no data folder", "broken mapfile", "no usable rows", "Mismatch",
                 "gzip_error", "format_error", "gzip error", "format error", "empty (0 reads)",
                 "missing from disk", "md5 mismatch"}
_ERROR_PARTS = ("corrupt", "empty", "in mapfile not on disk", "differ between Store")
_PENDING_LABELS = {"unchecked", "Not verified", "Not compared", "no files"}
_PENDING_PREFIXES = ("incomplete", "backup not yet verified", "unchecked (")
_TINT = {"error": "background-color: rgba(213, 94, 0, 0.30)",
         "warning": "background-color: rgba(230, 159, 0, 0.35)",
         "dismissed": "background-color: rgba(86, 180, 233, 0.35)",
         "pending": "background-color: rgba(187, 187, 187, 0.35)",
         "ok": ""}


def _severity(label):
    if not isinstance(label, str) or not label:
        return "ok"
    if label.lower() == "ok *" or label.endswith("dismissed)"):
        return "dismissed"
    s = label.removesuffix(" *")
    if s in _PENDING_LABELS or s.startswith(_PENDING_PREFIXES):
        return "pending"
    if s in _ERROR_LABELS or any(p in s for p in _ERROR_PARTS) or \
            re.search(r"\b\d+ missing\b", s):
        return "error"
    if s in ("flagged rows", "missing from mapfile") or "not in mapfile" in s or \
            all(t.strip() in ovalidate.FLAG_MESSAGES for t in s.split(";")):
        return "warning"
    return "ok"


def _tint(df, columns):
    if df.size > 100000:
        return df
    return df.style.map(lambda v: _TINT[_severity(v)],
                        subset=[c for c in columns if c in df.columns])


@st.cache_data(ttl=60)
def load_files(db_path, mtime):
    return _sql(db_path, f"""
        SELECT f.project_id, s.sample_id, f.direction, f.filename, f.rel_path,
               {_FULL_PATH} AS full_path,
               f.size_bytes, f.owner_name,
               CASE f.md5_match WHEN 1 THEN 'Verified' WHEN 0 THEN 'Mismatch'
                    ELSE 'Not compared' END AS backup,
               COALESCE(f.integrity_status, 'unchecked') AS integrity,
               f.n_reads, f.integrity_date
        FROM files f
        JOIN projects p ON p.project_id = f.project_id
        LEFT JOIN samples s ON s.sample_pk = f.sample_pk
        ORDER BY f.project_id, s.sample_id, f.direction""")


@st.cache_data(ttl=60)
def load_file_paths(db_path, mtime):
    """One row per file with its source root + rel_path, for cart export/rclone."""
    return _sql(db_path, f"""
        SELECT f.project_id, s.sample_id, f.direction, f.filename,
               p.seqdata_root, f.rel_path, {_FULL_PATH} AS full_path,
               f.size_bytes, f.n_reads,
               COALESCE(f.integrity_status, 'unchecked') AS integrity_status,
               EXISTS (SELECT 1 FROM data_check_issues i
                       WHERE i.project_id = f.project_id AND i.filename = f.rel_path
                         AND i.kind = 'missing from disk') AS missing_on_disk
        FROM files f
        JOIN projects p ON p.project_id = f.project_id
        LEFT JOIN samples s ON s.sample_pk = f.sample_pk
        ORDER BY f.project_id, s.sample_id, f.direction""")


def human_size(n):
    """Byte count reported in GB (e.g. 1.50 GB). Missing -> ''."""
    if n is None or pd.isna(n):
        return ""
    return f"{float(n) / 1024**3:.2f} GB"


def _contains(series, s):
    """Case-insensitive substring mask, safe on all-null columns.

    A column that is entirely NULL loads as float64, where `.str` raises. fillna +
    astype(str) normalizes to text first so search never crashes a view.
    """
    return series.fillna("").astype(str).str.lower().str.contains(s, na=False)


# Characters that make Excel/Sheets treat a cell as a formula (or a new cell); a
# leading one on an exported value could run code when the CSV is opened.
_CSV_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r", "\n")


def _csv_safe_cell(v):
    """Neutralize CSV formula injection: prefix a leading apostrophe to any string
    value that would start a spreadsheet formula. Non-strings (ints, floats, NaN,
    None) pass through unchanged so numeric columns keep their type."""
    if isinstance(v, str) and v[:1] in _CSV_FORMULA_LEADERS:
        return "'" + v
    return v


def _csv_safe_df(df):
    """Copy of df with string cells sanitized against CSV formula injection. Only
    string-dtype columns (object or str) are scanned, so numeric/Int64 columns are
    untouched."""
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_string_dtype(out[c].dtype):
            out[c] = out[c].map(_csv_safe_cell)
    return out


def _download(df, name):
    st.download_button("Download CSV", _csv_safe_df(df).to_csv(index=False).encode(),
                       name, "text/csv")


def _link_text(series):
    """Visible label of a LinkColumn(display_text=r'#(.+)$'): the text after the URL's
    '#'. Lets a per-column filter match what the user sees (e.g. 'Gadus morhua'), not
    the raw URL. Rows with no '#' become NaN (blank)."""
    return series.fillna("").astype(str).str.extract(r"#(.+)$", expand=False)


def _filter_by_columns(df, active, display=None):
    """Keep rows where every (col -> query) in `active` appears (case-insensitive
    substring) in that column's DISPLAYED value. `display` maps a column to the Series
    of shown values (e.g. a link label) searched instead of the raw column. All columns
    are preserved so downstream row selection still works."""
    display = display or {}
    mask = pd.Series(True, index=df.index)
    for c, val in active.items():
        ser = display[c] if c in display else df[c]
        mask &= ser.fillna("").astype(str).str.contains(val, case=False, na=False, regex=False)
    return df[mask]


def _column_filters(df, cols, key, labels=None, display=None):
    """Per-column substring filters shown as a row of boxes above a table.

    Streamlit can't put inputs inside a dataframe header, so this renders a row of
    small text boxes (one per column) in a collapsible 'Filter columns' bar just above
    the table. Each box is labeled with the column's DISPLAYED header (`labels`, falling
    back to the column name) and filters against its DISPLAYED value (`display`, falling
    back to the raw column) -- so a box over a link column matches the shown name, not
    the hidden URL. Returns df with rows kept only where every non-empty box's text
    appears (case-insensitive); all columns are preserved for downstream row selection.
    """
    labels = labels or {}
    with st.expander("🔍 Filter columns", expanded=False):
        boxes = st.columns(len(cols))
        active = {}
        for box, c in zip(boxes, cols):
            lab = labels.get(c, c)
            val = box.text_input(lab, key=f"colf_{key}_{c}", placeholder=lab,
                                 label_visibility="collapsed")
            if val.strip():
                active[c] = val.strip()
    return _filter_by_columns(df, active, display)


_CORRUPT = {"gzip_error": "gzip error", "format_error": "format error"}


def _file_issues(r):
    """[(column, kind)] for one files row's integrity/backup problems."""
    out = []
    if r["integrity"] in _CORRUPT:
        out.append(("integrity", _CORRUPT[r["integrity"]]))
    elif r["integrity"] == "ok" and r["n_reads"] == 0:
        out.append(("integrity", "empty (0 reads)"))
    if r["backup"] == "Mismatch":
        out.append(("backup", "md5 mismatch"))
    return out


def _mark_files(df, dismissed, show_dismissed):
    out = df.copy()
    if not dismissed:
        return out
    for idx, r in zip(df.index, df.to_dict("records")):
        for col, kind in _file_issues(r):
            if dismiss.file_key(r["project_id"], r["rel_path"], kind) in dismissed:
                out.at[idx, col] = kind + " (dismissed)" if show_dismissed else "OK *"
    return out


def _raw(show, src, cols):
    return show.assign(**{c: src.loc[show.index, c] for c in cols})


def _sample_flags_key(r):
    f = r["flags"]
    return dismiss.sample_key(r["project_id"], r["sample_id"], f) \
        if isinstance(f, str) and f.strip() else None


def samples_view(df, files_df):
    with st.sidebar:
        st.header("Filters")
        projects = sorted(df["project_id"].unique())
        chosen = st.multiselect("Project", projects)
        backup = st.selectbox("Backup status", ["All", "Verified", "Not verified"])
        search = st.text_input("Search (sample, taxon, UniqID)")
        dismissed, show_dismissed, available, log_path = _dismiss_sidebar()

    view = df
    if chosen:
        view = view[view["project_id"].isin(chosen)]
    if backup == "Verified":
        view = view[view["backup_verified"] == 1]
    elif backup == "Not verified":
        view = view[view["backup_verified"] != 1]
    if search:
        s = search.lower()
        mask = (_contains(view["sample_id"], s) | _contains(view["taxon"], s)
                | _contains(view["uniq_id"], s) | _contains(view["worms_name"], s))
        view = view[mask]

    show = view.copy()
    show["backup"] = show["backup_verified"].map(backup_label)
    for idx, r in zip(show.index, show.to_dict("records")):
        if _sample_flags_key(r) in dismissed:
            show.at[idx, "flags"] = r["flags"] + " (dismissed)" if show_dismissed else "OK *"
    on_screen = ["project_id", "sample_id", "taxon",
                 "ncbi_url", "tax_match", "worms_url", "worms_match", "worms_status",
                 "uniq_id", "uniq_id_url", "flags", "backup"]
    tax_lab = show["tax_match"].map(MATCH_LABELS).fillna(show["tax_match"])
    worms_lab = show["worms_match"].map(WORMS_MATCH_LABELS).fillna(show["worms_match"])
    # Label each filter box with the header the user sees, and search the DISPLAYED
    # value: the link columns show a name (after the URL '#'), not the raw URL; the
    # UniqID link icon has no text, so filter it by the adjacent UniqID value.
    show = _column_filters(
        show, on_screen, "samples",
        labels={"ncbi_url": "NCBI_name", "tax_match": "NCBI_match",
                "worms_url": "WoRMS_name", "worms_match": "WoRMS_match",
                "worms_status": "WoRMS_status", "uniq_id_url": "uniq_id_link"},
        display={"ncbi_url": _link_text(show["ncbi_url"]),
                 "worms_url": _link_text(show["worms_url"]),
                 "tax_match": tax_lab, "worms_match": worms_lab,
                 "uniq_id_url": show["uniq_id"]})
    st.caption(f"{len(show)} of {len(df)} samples (full R1/R2 paths, taxid + "
               "lineage are in the CSV export). Select a row to list its files below. "
               "* an issue here was dismissed; turn on 'Show dismissed' to see it.")
    disp = show[on_screen].assign(tax_match=tax_lab.loc[show.index],
                                  worms_match=worms_lab.loc[show.index])
    event = st.dataframe(
        _tint(disp.reset_index(drop=True), ["flags", "backup"]), width="stretch",
        hide_index=True, on_select="rerun", selection_mode="single-row",
        key=f"samples_table_{hash(tuple(show.index))}",
        column_config={
            # Each source's matched name is a link (label = the name, carried in the
            # URL '#'-fragment) to its taxonomy page; the "* match" column is that
            # source's match confidence, so NCBI vs WoRMS fuzziness is never conflated.
            "ncbi_url": st.column_config.LinkColumn("NCBI_name", display_text=r"#(.+)$"),
            "tax_match": "NCBI_match",
            "worms_url": st.column_config.LinkColumn("WoRMS_name", display_text=r"#(.+)$"),
            "worms_match": "WoRMS_match",
            "worms_status": "WoRMS_status",
            "uniq_id_url": _uniqid_link_col()})
    _download(_raw(show, df, ["flags"]), f"{_config(DB_PATH, 'catalog_slug')}_samples.csv")

    sel = event.selection.rows if event and event.selection else []
    sel = [i for i in sel if i < len(show)]
    if not sel:
        return
    r = show.iloc[sel[0]]
    pid, sid = r["project_id"], r["sample_id"]
    fkey = _sample_flags_key(df.loc[r.name]) if available else None
    if fkey and fkey not in dismissed:
        note = st.text_input("Note", key="note_flags")
        if st.button("Dismiss flags for this sample") and \
                _record(log_path, "dismiss", [fkey], note):
            st.rerun()
    elif fkey and show_dismissed and st.button("Undo") and \
            _record(log_path, "undo", [fkey], ""):
        st.rerun()
    fsub = _mark_files(files_df[(files_df["project_id"] == pid)
                                & (files_df["sample_id"] == sid)], dismissed, show_dismissed)
    st.subheader(f"Files for sample {sid} ({pid})")
    if len(fsub):
        fsub["size"] = fsub["size_bytes"].map(human_size)
        st.dataframe(
            _tint(fsub[["direction", "filename", "full_path", "size", "owner_name",
                        "backup", "integrity", "n_reads", "integrity_date"]]
                  .reset_index(drop=True), ["backup", "integrity"]),
            width="stretch", hide_index=True,
            column_config={"n_reads": "reads", "integrity_date": "checked"})
        _download(_raw(fsub, files_df, ["backup", "integrity"]), f"{sid}_files.csv")
    else:
        st.info("No files cataloged for this sample.")


_STATUS_COLS = ["mapfile", "data_files", "checksum", "integrity"]


def projects_view(df, issues, integ_issues, md5_issues):
    with st.sidebar:
        st.header("Filters")
        search = st.text_input("Search (project, description)")
        mapfile_only = st.checkbox("Only mapfile issues")
        data_only = st.checkbox("Only data-files issues")
        cs_only = st.checkbox("Only checksum issues")
        integ_only = st.checkbox("Only integrity issues")
        dismissed, show_dismissed, available, log_path = _dismiss_sidebar()
    all_issues = _mark_issues(pd.concat([issues, integ_issues, md5_issues], ignore_index=True),
                              dismissed)
    by_pid = dict(list(all_issues.groupby("project_id")))
    def statuses(show):
        return pd.DataFrame([_project_status(r, by_pid.get(r["project_id"], all_issues.iloc[:0]),
                                             dismissed, show) for r in df.to_dict("records")],
                            columns=_STATUS_COLS, index=df.index)

    live_lab = statuses(False)
    view = df.join(statuses(True) if show_dismissed else live_lab)
    if search:
        s = search.lower()
        mask = (_contains(view["project_id"], s) | _contains(view["description"], s))
        view = view[mask]

    def bad(col):
        return live_lab.loc[view.index, col].map(_severity).isin(["error", "warning"])

    if mapfile_only:
        view = view[~live_lab.loc[view.index, "mapfile"].astype(str).str.startswith("OK")]
    if data_only:
        view = view[bad("data_files")]
    if cs_only:
        view = view[bad("checksum") | (view["n_uncompared"].fillna(0) > 0)]
    if integ_only:
        view = view[bad("integrity")]

    show = view.copy()
    cols = ["project_id", "source", "description", "n_samples", "n_files",
            "mapfile", "data_files", "checksum", "integrity", "owner_name", "data_dir",
            "date_ingested"]
    show = _column_filters(show, cols, "projects")
    st.caption(f"{len(show)} of {len(df)} projects. 'mapfile' flags a folder with no "
               "mapfile, a mapfile with no folder, a broken mapfile, flagged rows, or "
               "no usable rows. Select a row for "
               "its mapfile explanation + data-files & integrity issues below. "
               "'owner_name' is the filesystem owner (a personal identifier). "
               "* an issue here was dismissed; turn on 'Show dismissed' to see it.")
    event = st.dataframe(_tint(show[cols].reset_index(drop=True), _STATUS_COLS),
                         width="stretch", hide_index=True,
                         on_select="rerun", selection_mode="single-row",
                         key=f"projects_table_{hash(tuple(show.index))}")
    _download(show, f"{_config(DB_PATH, 'catalog_slug')}_projects.csv")

    sel = event.selection.rows if event and event.selection else []
    sel = [i for i in sel if i < len(show)]
    if not sel:
        return
    prow = show.iloc[sel[0]]
    pid = prow["project_id"]
    mstatus = prow["metadata_status"]
    if isinstance(mstatus, str) and mstatus != "ok":
        mkey = dismiss.project_key(pid, mstatus, prow["metadata_detail"])
        hit = dismissed.get(mkey)
        if hit is None:
            st.warning(f"**Mapfile issue ({_MAPFILE_LABEL.get(mstatus, mstatus)}):** "
                       f"{prow['metadata_detail']}")
            if available:
                note = st.text_input("Note", key="note_mapfile")
                if st.button("Dismiss mapfile issue") and \
                        _record(log_path, "dismiss", [mkey], note):
                    st.rerun()
        else:
            extra = ": " + _md(hit["note"]) if hit["note"] else ""
            st.info(f"Mapfile: OK * (dismissed by {_md(hit['user'])} on "
                    f"{hit['time'][:10]}{extra})")
            if available and show_dismissed and st.button("Undo") and \
                    _record(log_path, "undo", [mkey], ""):
                st.rerun()

    # Disk/mapfile checks, integrity failures, and md5 mismatches live in different
    # tables but answer one question ("what's wrong with this project's files?").
    sub = all_issues[all_issues["project_id"] == pid].sort_values(
        ["kind", "filename"], kind="stable")
    live = sub[~sub["dismissed"]]
    rows = (sub if show_dismissed else live).reset_index(drop=True)
    st.subheader(f"Data-files issues: {pid}")
    if not len(rows):
        if len(sub):
            st.info(f"{len(sub)} issue(s) dismissed; turn on 'Show dismissed' to see them.")
        else:
            st.info("No recorded data-files or integrity issues. Run "
                    "`validate --seqdata-root` and `integrity --collect` to refresh.")
        return
    rows["issue"] = rows["kind"].map(_ISSUE_LABEL).fillna(rows["kind"])
    rows.loc[rows["dismissed"], "issue"] += " (dismissed)"
    rows["detail"] = rows["kind"].map(_ISSUE_DETAIL).fillna("")
    icols = ["issue", "detail", "filename", "full_path", "sample_id",
             "direction", "taxon", "uniq_id", "uniq_id_url"]
    if show_dismissed:
        icols += ["dismissed_by", "dismissed_on", "note"]
    ev = st.dataframe(
        _tint(rows[icols], ["issue"]), width="stretch", hide_index=True,
        on_select="rerun", selection_mode="multi-row", key=f"issues_table_{pid}_{hash(tuple(zip(rows['filename'], rows['kind'])))}",
        column_config={"full_path": "expected_path",
                       "uniq_id_url": _uniqid_link_col()})
    _download(rows, f"{pid}_data_issues.csv")
    if not available:
        return
    picked = rows.iloc[[i for i in (ev.selection.rows if ev and ev.selection else [])
                        if i < len(rows)]]
    live_sel, dis_sel = picked[~picked["dismissed"]], picked[picked["dismissed"]]

    def keys(frame):
        return [_issue_key(r) for r in frame.to_dict("records")]

    note = st.text_input("Note", key="note_issues")
    if st.button(f"Dismiss selected ({len(live_sel)})", disabled=live_sel.empty) and \
            _record(log_path, "dismiss", keys(live_sel), note):
        st.rerun()
    if st.button(f"Dismiss all ({len(live)})", disabled=live.empty) and \
            _record(log_path, "dismiss", keys(live), note):
        st.rerun()
    if show_dismissed and \
            st.button(f"Undo selected ({len(dis_sel)})", disabled=dis_sel.empty) and \
            _record(log_path, "undo", keys(dis_sel), ""):
        st.rerun()


def files_view(df, samples_df):
    with st.sidebar:
        st.header("Filters")
        projects = sorted(df["project_id"].unique())
        chosen = st.multiselect("Project", projects)
        backup = st.selectbox("Backup status", ["All", "Verified", "Mismatch", "Not compared"])
        integrity = st.selectbox(
            "Integrity", ["All", "ok", "gzip_error", "format_error", "unchecked"])
        search = st.text_input("Search (sample, filename)")
        dismissed, show_dismissed, available, log_path = _dismiss_sidebar()
    live = _mark_files(df, dismissed, False)
    view = _mark_files(df, dismissed, True) if show_dismissed else live
    if chosen:
        view = view[view["project_id"].isin(chosen)]
    if backup != "All":
        view = view[live.loc[view.index, "backup"] == backup]
    if integrity != "All":
        view = view[live.loc[view.index, "integrity"] == integrity]
    if search:
        s = search.lower()
        mask = (_contains(view["sample_id"], s) | _contains(view["filename"], s))
        view = view[mask]
    show = view.copy()
    show["size"] = show["size_bytes"].map(human_size)
    on_screen = ["project_id", "sample_id", "direction", "filename", "full_path",
                 "size", "owner_name", "backup", "integrity", "n_reads",
                 "integrity_date"]
    show = _column_filters(show, on_screen, "files",
                           labels={"n_reads": "reads", "integrity_date": "checked"})
    st.caption(f"{len(show)} of {len(df)} files. "
               "Select a row to show its sample info below. "
               "'owner_name' is the filesystem owner (a personal identifier). "
               "* an issue here was dismissed; turn on 'Show dismissed' to see it.")
    event = st.dataframe(
        _tint(show[on_screen].reset_index(drop=True), ["backup", "integrity"]),
        width="stretch", hide_index=True,
        on_select="rerun", selection_mode="single-row",
        key=f"files_table_{hash(tuple(show.index))}",
        column_config={"n_reads": "reads", "integrity_date": "checked"})
    _download(_raw(show, df, ["backup", "integrity"]),
              f"{_config(DB_PATH, 'catalog_slug')}_files.csv")

    sel = event.selection.rows if event and event.selection else []
    sel = [i for i in sel if i < len(show)]
    if not sel:
        return
    r = show.iloc[sel[0]]
    pid, sid = r["project_id"], r["sample_id"]
    keys = [(kind, dismiss.file_key(pid, r["rel_path"], kind))
            for _, kind in _file_issues(df.loc[r.name])] if available else []
    live_keys = [(k, key) for k, key in keys if key not in dismissed]
    if live_keys:
        note = st.text_input("Note", key="note_file")
        for kind, key in live_keys:
            if st.button(f"Dismiss {kind}") and _record(log_path, "dismiss", [key], note):
                st.rerun()
    if show_dismissed:
        for kind, key in keys:
            if key in dismissed and st.button(f"Undo {kind}") and \
                    _record(log_path, "undo", [key], ""):
                st.rerun()
    ssub = samples_df[(samples_df["project_id"] == pid)
                      & (samples_df["sample_id"] == sid)]
    st.subheader(f"Sample info: {sid} ({pid})")
    if len(ssub):
        ssub = ssub.copy()
        ssub["backup"] = ssub["backup_verified"].map(backup_label)
        cols = [c for c in ["sample_id", "taxon", "ncbi_url", "tax_match",
                            "taxid", "lineage", "worms_url", "worms_match",
                            "worms_status", "uniq_id", "uniq_id_url",
                            "backup", "r1_path", "r2_path"] if c in ssub.columns]
        st.dataframe(
            ssub[cols], width="stretch", hide_index=True,
            column_config={
                "ncbi_url": st.column_config.LinkColumn("NCBI_name", display_text=r"#(.+)$"),
                "tax_match": "NCBI_match",
                "worms_url": st.column_config.LinkColumn("WoRMS_name", display_text=r"#(.+)$"),
                "worms_match": "WoRMS_match",
                "worms_status": "WoRMS_status",
                "uniq_id_url": _uniqid_link_col()})
        _download(ssub, f"{sid}_sample.csv")
    else:
        st.info("No sample record for this file "
                "(it may be an orphan not tied to a cataloged sample).")


def _drop_unknown(v, rank_cols, depth):
    """Exclude samples whose deepest selected rank is unranked ('unknown').

    Shared by all three taxonomy charts so clearing 'Include unknown' removes the SAME
    sample population from each: the sunburst always filtered on this, but the
    resolution-quality and composition charts used to count everyone. Empty/NULL at the
    deepest rank normalizes to 'unknown', matching how the sunburst labels the wedge.
    """
    kept = v[rank_cols[depth - 1]].replace("", pd.NA).fillna("unknown")
    return v[kept != "unknown"]


@st.cache_data(ttl=60)
def _taxonomy_counts(db_path, mtime, source, chosen, depth, include_unknown):
    """Per-lineage sample counts for the sunburst, cached across reruns.

    Grouping ~6k samples over up to 8 rank columns is the per-interaction cost;
    caching on (source, projects, depth, include_unknown) keeps filter/slider
    changes from re-grouping every rerun. Returns (counts_df, n_samples, n_projects).
    """
    df = load_taxonomy(db_path, mtime)
    cols = SOURCES[source]["rank_cols"][:depth]
    v = df if not chosen else df[df["project_id"].isin(list(chosen))]
    v = v[["project_id"] + cols].copy()
    for c in cols:
        v[c] = v[c].replace("", pd.NA).fillna("unknown")
    if not include_unknown:
        v = v[v[cols[-1]] != "unknown"]
    counts = v.groupby(cols, sort=False).size().reset_index(name="samples")
    return counts, len(v), v["project_id"].nunique()


@st.cache_data(ttl=60)
def _matchtype_counts(db_path, mtime, source, chosen, depth, include_unknown):
    """Sample counts by taxonomy match_type (resolution confidence)."""
    df = load_taxonomy(db_path, mtime)
    s = SOURCES[source]
    v = df if not chosen else df[df["project_id"].isin(list(chosen))]
    if not include_unknown:
        v = _drop_unknown(v, s["rank_cols"], depth)
    mt = v[s["match_col"]].replace("", pd.NA).fillna("unresolved")
    counts = mt.value_counts().rename_axis("match_type").reset_index(name="samples")
    counts["order"] = counts["match_type"].map(
        {m: i for i, m in enumerate(s["match_order"])}).fillna(len(s["match_order"]))
    counts["label"] = counts["match_type"].map(s["match_labels"]).fillna(counts["match_type"])
    return counts.sort_values("order")


@st.cache_data(ttl=60)
def _composition_counts(db_path, mtime, chosen, rank_col, source, depth, include_unknown):
    """Per-project sample counts within one rank (for the stacked composition bar)."""
    df = load_taxonomy(db_path, mtime)
    v = df if not chosen else df[df["project_id"].isin(list(chosen))]
    if not include_unknown:
        v = _drop_unknown(v, SOURCES[source]["rank_cols"], depth)
    v = v[["project_id", rank_col]].copy()
    v[rank_col] = v[rank_col].replace("", pd.NA).fillna("unknown")
    return v.groupby(["project_id", rank_col], sort=False).size().reset_index(name="samples")


def taxonomy_view(db_path, mtime):
    try:
        import plotly.express as px
    except ImportError:
        st.error("The Taxonomy view needs plotly. Install it with: conda env update "
                 "-f environment.yml  (or: pip install 'seqledger[gui]')")
        return

    df = load_taxonomy(db_path, mtime)  # cached; used only for the project list
    with st.sidebar:
        st.header("Filters")
        source = st.radio("Taxonomy source", list(SOURCES), horizontal=True,
                          help="NCBI (local taxdump) or WoRMS (World Register of "
                               "Marine Species). Populate WoRMS with "
                               "`taxonomy resolve --source worms`.")
        rank_labels = SOURCES[source]["rank_labels"]
        rank_cols = SOURCES[source]["rank_cols"]
        projects = sorted(df["project_id"].unique())
        chosen = st.multiselect("Project", projects)
        chart_type = st.radio("Hierarchy chart", ["Sunburst", "Treemap"], horizontal=True)
        depth_label = st.selectbox("Deepest rank", rank_labels,
                                   index=rank_labels.index("order"))
        include_unknown = st.checkbox("Include 'unknown'", value=True)
        ring_cap = st.slider(
            "Levels shown", 2, len(rank_cols), min(4, len(rank_cols)),
            help="Rank levels drawn per step (fewer = faster). Both charts drill on "
                 "click: Sunburst zooms client-side; Treemap re-roots deeper. Clicking "
                 "also scopes the bar below.")
        comp_label = st.selectbox("Composition rank (stacked bar)", rank_labels,
                                  index=rank_labels.index("phylum"))

    depth = rank_labels.index(depth_label) + 1
    cols = rank_cols[:depth]
    counts, n_samples, n_proj = _taxonomy_counts(
        db_path, mtime, source, tuple(chosen), depth, include_unknown)

    resolve_cmd = "taxonomy resolve" + (" --source worms" if source == "WoRMS" else "")
    st.caption(f"{source}: {n_samples} samples across {n_proj} project(s); "
               f"run `{resolve_cmd}` to (re)populate. Unranked -> 'unknown'.")
    if not n_samples:
        if df.empty:
            st.info("No samples in this catalog yet.")
        else:
            st.info(f"No resolved {source} taxonomy yet. Run `seqledger {resolve_cmd}`.")
        return

    # --- Hierarchy chart: sunburst or treemap ---
    # px.sunburst/treemap send + lay out EVERY node in `path`, so a species-deep
    # tree is thousands of nodes. The two charts handle depth differently:
    #  - Sunburst: cheap radial arcs. Pre-load the full depth, draw `ring_cap` rings
    #    (maxdepth); clicking a wedge drills deeper client-side, no rerun.
    #  - Treemap: its squarified per-tile layout is costly at scale (the reported
    #    slowness), so it uses server-side PROGRESSIVE drill: it renders only
    #    `ring_cap` levels below a movable root; clicking a tile re-roots deeper (a
    #    small, fast rebuild), so you can reach species without ever loading it all.
    st.subheader(f"Taxonomic breadth ({chart_type.lower()})")
    if chart_type == "Treemap":
        lineage, sub = _treemap_drilldown(px, counts, cols, depth, ring_cap)
    else:
        fig = px.sunburst(counts, path=cols, values="samples")
        fig.update_traces(maxdepth=min(ring_cap, depth))  # draw ring_cap rings; drill deeper on click
        fig.update_layout(margin=dict(t=10, l=10, r=10, b=10))
        event = st.plotly_chart(fig, width="stretch", on_select="rerun", key="hierarchy")
        lineage = _selected_lineage(event, counts, cols)
        sub = counts
        for col, val in zip(cols, lineage):
            sub = sub[sub[col] == val]

    if lineage:
        st.caption("Bar chart scoped to: " + " › ".join(lineage))
    st.subheader(f"Sample count by {depth_label}")
    if sub.empty:
        st.info(f"No {depth_label}-level samples under this selection.")
    else:
        bar = (sub.groupby(cols[-1])["samples"].sum().rename_axis(depth_label)
               .reset_index(name="samples").set_index(depth_label))
        st.bar_chart(bar, width="stretch")
    _download(counts, f"{_config(DB_PATH, 'catalog_slug')}_taxonomy_counts.csv")

    # --- Resolution quality: how confidently taxa were identified ---
    st.divider()
    st.subheader("Resolution quality")
    st.caption(f"How each sample's taxon resolved {SOURCES[source]['blurb']}: "
               "confirmed/exact are reliable; the rest are best-guess matches; "
               "unresolved had no match.")
    mt = _matchtype_counts(db_path, mtime, source, tuple(chosen), depth, include_unknown)
    fig_mt = px.bar(mt, x="samples", y="label", orientation="h",
                    color="match_type", color_discrete_map=SOURCES[source]["match_colors"],
                    category_orders={"label": list(mt["label"])[::-1]})
    fig_mt.update_layout(showlegend=False, yaxis_title=None, xaxis_title="samples",
                         margin=dict(t=6, l=6, r=6, b=6), height=max(160, 34 * len(mt)))
    st.plotly_chart(fig_mt, width="stretch", key="matchtype")

    # --- Composition by project: stacked sample counts within a coarse rank ---
    st.divider()
    st.subheader(f"Composition by project ({comp_label})")
    rank_col = rank_cols[rank_labels.index(comp_label)]
    comp = _composition_counts(db_path, mtime, tuple(chosen), rank_col,
                               source, depth, include_unknown)
    n_cat = comp[rank_col].nunique()
    st.caption(f"Sample counts per project, colored by {comp_label} "
               f"({n_cat} group(s)). Pick a coarser rank if the legend is too busy.")
    fig_c = px.bar(comp, x="project_id", y="samples", color=rank_col,
                   color_discrete_sequence=px.colors.qualitative.Safe)
    fig_c.update_layout(barmode="stack", xaxis_title=None, legend_title=comp_label,
                        margin=dict(t=6, l=6, r=6, b=6))
    st.plotly_chart(fig_c, width="stretch", key="composition")


def _selected_lineage(event, counts, cols):
    """Ancestor path (root->clicked wedge) of the sunburst selection, or [].

    Returns the list of rank values from the outermost ring down to the clicked
    wedge; an empty list means nothing (or the root) is selected, so the bar
    chart shows everything.
    """
    try:
        points = event.selection["points"]
    except (AttributeError, KeyError, TypeError):
        return []
    if not points:
        return []
    pt = points[0]
    # Preferred: the wedge id is the "/"-joined lineage (e.g. "Eukaryota/Animalia").
    wid = pt.get("id") or pt.get("label")
    if not wid:
        return []
    lineage = [p for p in str(wid).split("/") if p]
    # Guard against a stale/ambiguous id: keep it only if it names a real subtree.
    for col, val in zip(cols, lineage):
        if not (counts[col] == val).any():
            # id form didn't line up; try matching the bare label to one column.
            label = pt.get("label")
            for c in cols:
                if label is not None and (counts[c] == label).any():
                    return list(counts.loc[counts[c] == label, cols[:cols.index(c) + 1]]
                                .iloc[0])
            return []
    return lineage[:len(cols)]


def _treemap_drilldown(px, counts, cols, depth, ring_cap):
    """Progressive treemap: render `ring_cap` levels below a movable root; clicking a
    tile re-roots deeper so you can reach species without ever loading the full tree.

    Returns (root_lineage, subtree_counts) -- the current root path and the counts
    filtered to it, for the detail bar below. The root persists in session_state and
    is re-validated against the (possibly re-filtered) counts each run.
    """
    root = list(st.session_state.get("tm_root", []))
    sub = counts
    valid = []
    for i, val in enumerate(root):
        if i < depth and (sub[cols[i]] == val).any():
            sub = sub[sub[cols[i]] == val]
            valid.append(val)
        else:
            break  # filters changed under us -> truncate the root to what still exists
    root = valid
    st.session_state["tm_root"] = root

    if root:
        nav = st.columns([1, 1, 6])
        if nav[0].button("⬆ Up", key="tm_up"):
            st.session_state["tm_root"] = root[:-1]
            st.rerun()
        if nav[1].button("⌂ Top", key="tm_reset"):
            st.session_state["tm_root"] = []
            st.rerun()
        nav[2].caption("Rooted at: " + " › ".join(root) + " — click a tile to drill deeper.")
    else:
        st.caption("Click a tile to drill into that clade (down to species, without "
                   "loading the whole tree).")

    start = len(root)
    tm_cols = cols[start:min(start + ring_cap, depth)]
    if not tm_cols:
        st.info("At the deepest rank for this selection — go Up, or raise 'Deepest rank'.")
        return root, sub

    tm_data = sub.groupby(tm_cols, sort=False)["samples"].sum().reset_index()
    fig = px.treemap(tm_data, path=tm_cols, values="samples")
    fig.update_layout(margin=dict(t=10, l=10, r=10, b=10))
    event = st.plotly_chart(fig, width="stretch", on_select="rerun", key="hierarchy")

    clicked = _selected_lineage(event, tm_data, tm_cols)
    if clicked:
        # Drill on a NEW click only; a stored selection can replay on rerun, so a
        # dedup token (current root + click) prevents an endless re-root loop.
        token = (tuple(root), tuple(clicked))
        if st.session_state.get("tm_last") != token:
            st.session_state["tm_last"] = token
            st.session_state["tm_root"] = root + list(clicked)
            st.rerun()
    return root, sub


def _match(series, query, use_regex):
    """Case-insensitive filter mask for a query over a string column.

    Substring match by default; full regex when use_regex. Invalid regex raises
    re.error, which the caller surfaces to the user.
    """
    return series.fillna("").str.contains(query, case=False, regex=use_regex, na=False)


# Best-effort guards for user-supplied regex on the shared, tunneled process. stdlib
# `re` has no match timeout, so true ReDoS immunity isn't possible; capping the pattern
# length and rows scanned bounds the blast radius of a catastrophic-backtracking pattern.
_MAX_REGEX_LEN = 250
_MAX_REGEX_ROWS = 5000


def _validate_regex(pattern):
    """Validate a user regex before it filters the Grab & Go table.

    Returns (ok, error): rejects an over-long pattern and one that won't compile.
    This is a best-effort guard, NOT ReDoS-proof -- a short pattern can still
    backtrack pathologically; the row cap in the caller bounds that exposure.
    """
    if len(pattern) > _MAX_REGEX_LEN:
        return False, f"pattern too long (max {_MAX_REGEX_LEN} characters)"
    try:
        re.compile(pattern)
    except re.error as e:
        return False, f"invalid regex: {e}"
    return True, None


def _cart_key(project_id, sample_id):
    return f"{project_id}\t{sample_id}"


_PROBLEM_KINDS = ("corrupt", "empty", "missing")
_SCRIPT_PREVIEW_FILES = 20


def _file_problem(r, dismissed=frozenset()):
    """One file's state for the cart: missing | corrupt | empty | ok | unchecked | dismissed."""
    status = r.get("integrity_status")
    if r.get("missing_on_disk"):
        problem, kind = "missing", "missing from disk"
    elif status in ("gzip_error", "format_error"):
        problem, kind = "corrupt", status.replace("_", " ")
    elif status == "ok":
        n = r.get("n_reads")
        if n is not None and not pd.isna(n) and n == 0:
            problem, kind = "empty", "empty (0 reads)"
        else:
            return "ok"
    else:
        return "unchecked"
    if dismissed and dismiss.file_key(r["project_id"], r["rel_path"], kind) in dismissed:
        return "dismissed"
    return problem


def _files_ok(problems):
    """Per-sample summary of its files' _file_problem values."""
    problems = list(problems)
    if not problems:
        return "no files"
    parts = [f"{problems.count(k)} {k}" for k in _PROBLEM_KINDS if k in problems]
    if "unchecked" in problems:
        parts.append("unchecked")
    out = ", ".join(parts) or "ok"
    return out + " *" if "dismissed" in problems else out


def custom_table_view(samples_df, paths_df):
    """Build a custom sample table by searching + selecting, then export / copy.

    Selections persist in st.session_state['cart'] across views and reruns. From
    the collected samples the user can download a CSV and generate an rclone copy
    job (lTIO.sq submission script) with a disk-space estimate.
    """
    import re

    cart = st.session_state.setdefault("cart", {})  # cart_key -> {project_id, sample_id}

    with st.sidebar:
        st.header("Search")
        use_regex = st.toggle("Regex search", value=False,
                              help="Match the text fields as regular expressions.")
        projects = sorted(samples_df["project_id"].unique())
        f_projects = st.multiselect("Project", projects)
        f_taxon = st.text_input("Taxonomy (taxon / NCBI name / lineage)")
        f_sample = st.text_input("Sample ID")
        f_uniq = st.text_input("UniqID")

    view = samples_df
    if f_projects:
        view = view[view["project_id"].isin(f_projects)]
    if use_regex:
        # Validate each pattern up front, and cap the rows a regex ever scans so a
        # catastrophic pattern can't freeze the shared process for all tunneled users.
        for label, q in (("Taxonomy", f_taxon), ("Sample ID", f_sample),
                         ("UniqID", f_uniq)):
            if q:
                ok, err = _validate_regex(q)
                if not ok:
                    st.error(f"{label} search — {err}")
                    return
        if len(view) > _MAX_REGEX_ROWS:
            st.warning(f"Regex search is limited to the first {_MAX_REGEX_ROWS} rows "
                       "for responsiveness — add a Project filter to narrow the set.")
            view = view.head(_MAX_REGEX_ROWS)
    try:
        if f_taxon:
            view = view[_match(view["taxon"], f_taxon, use_regex)
                        | _match(view["tax_name"], f_taxon, use_regex)
                        | _match(view["lineage"], f_taxon, use_regex)]
        if f_sample:
            view = view[_match(view["sample_id"], f_sample, use_regex)]
        if f_uniq:
            view = view[_match(view["uniq_id"], f_uniq, use_regex)]
    except re.error as e:
        st.error(f"Invalid regex: {e}")
        return
    except Exception as e:  # a pattern re accepts but chokes on at match time
        st.error(f"Search failed: {e}")
        return

    st.subheader("Search results")
    st.caption(f"{len(view)} match(es). Select rows and click **Add selected**. "
               f"Grab & Go table currently holds {len(cart)} sample(s).")
    cols = ["project_id", "sample_id", "taxon", "tax_name", "uniq_id", "uniq_id_url"]
    event = st.dataframe(
        view[cols], width="stretch", hide_index=True,
        on_select="rerun", selection_mode="multi-row", key="cart_search",
        column_config={"tax_name": "NCBI_name", "uniq_id_url": _uniqid_link_col()})

    sel = event.selection.rows if event and event.selection else []
    c1, c2, c3 = st.columns(3)
    if c1.button(f"➕ Add selected ({len(sel)})", disabled=not sel):
        for i in sel:
            r = view.iloc[i]
            cart[_cart_key(r["project_id"], r["sample_id"])] = {
                "project_id": r["project_id"], "sample_id": r["sample_id"]}
        st.rerun()
    if c2.button(f"➕ Add all matches ({len(view)})", disabled=not len(view)):
        for _, r in view.iterrows():
            cart[_cart_key(r["project_id"], r["sample_id"])] = {
                "project_id": r["project_id"], "sample_id": r["sample_id"]}
        st.rerun()
    if c3.button("🗑 Clear table", disabled=not cart):
        cart.clear()
        st.rerun()

    st.divider()
    st.subheader(f"Grab & Go table ({len(cart)} sample(s))")
    if not cart:
        st.info("No samples yet. Search above and add some.")
        return

    keyset = {(v["project_id"], v["sample_id"]) for v in cart.values()}
    idx = samples_df.set_index(["project_id", "sample_id"]).index
    table = samples_df[idx.isin(keyset)].copy()

    # Combined size of each sample's sequence files (R1 + R2 + any extras), in GB.
    # min_count=1 keeps samples with no recorded sizes as NaN (blank), not 0.00 GB.
    sizes = (paths_df.groupby(["project_id", "sample_id"])["size_bytes"]
             .sum(min_count=1).rename("total_bytes").reset_index())
    table = table.merge(sizes, on=["project_id", "sample_id"], how="left")
    table["total_size"] = table["total_bytes"].map(human_size)

    # R1 and R2 read counts per sample (summed across lane-split files), reported as
    # separate columns so any R1/R2 parity mismatch is visible. A NULL n_reads (an
    # unchecked file) keeps that direction's total blank rather than undercounting;
    # nullable Int64 renders whole counts without a trailing .0.
    for _dir, _col in (("R1", "r1_reads"), ("R2", "r2_reads")):
        counts = (paths_df[paths_df["direction"] == _dir]
                  .groupby(["project_id", "sample_id"])["n_reads"]
                  .sum(min_count=1).rename(_col).reset_index())
        table = table.merge(counts, on=["project_id", "sample_id"], how="left")
        table[_col] = table[_col].astype("Int64")

    # Files backing the cart samples, each tagged corrupt/empty/missing/ok/unchecked/dismissed.
    fpaths = paths_df[paths_df.set_index(["project_id", "sample_id"]).index.isin(keyset)].copy()
    _, dismissed, _ = _dismiss_state()
    fpaths["problem"] = [_file_problem(r, dismissed) for r in fpaths.to_dict("records")]
    ok_by = {k: _files_ok(g) for k, g in fpaths.groupby(["project_id", "sample_id"])["problem"]}
    table["files_ok"] = [ok_by.get(k, "no files")
                         for k in zip(table["project_id"], table["sample_id"])]

    tcols = ["project_id", "sample_id", "taxon",
             "ncbi_url", "taxid", "tax_match",
             "worms_url", "worms_match", "worms_status",
             "total_size", "r1_reads", "r2_reads", "files_ok",
             "uniq_id", "uniq_id_url", "r1_path", "r2_path"]
    tcols = [c for c in tcols if c in table.columns]
    tevent = st.dataframe(
        _tint(table[tcols], ["files_ok"]), width="stretch", hide_index=True,
        on_select="rerun", selection_mode="multi-row", key="cart_table",
        column_config={
            "ncbi_url": st.column_config.LinkColumn("NCBI_name", display_text=r"#(.+)$"),
            "taxid": "NCBI_taxid",
            "tax_match": "NCBI_match",
            "worms_url": st.column_config.LinkColumn("WoRMS_name", display_text=r"#(.+)$"),
            "worms_match": "WoRMS_match",
            "worms_status": "WoRMS_status",
            "total_size": "seq_data_size",
            "uniq_id_url": _uniqid_link_col()})
    tsel = tevent.selection.rows if tevent and tevent.selection else []
    if st.button(f"➖ Remove selected ({len(tsel)})", disabled=not tsel):
        for i in tsel:
            r = table.iloc[i]
            cart.pop(_cart_key(r["project_id"], r["sample_id"]), None)
        st.rerun()
    bad = fpaths["problem"].isin(_PROBLEM_KINDS)
    if bad.any():
        st.warning(f"{int(bad.sum())} of {len(fpaths)} files in the cart are corrupt, empty, "
                   "or missing on disk; they will fail to copy or copy bad data.")
        if st.checkbox("Exclude problem files from the copy job and map", value=True):
            fpaths = fpaths[~bad]
    rows = fpaths.to_dict("records")

    id_choices = {"Sample ID": "sample_id", "UniqID": "uniq_id"}

    # Export uses plain, source-labeled headers (NCBI vs WoRMS) instead of the
    # display's link columns; 'taxon' is the sample's raw string (neither source).
    export_map = {
        "project_id": "project_id", "sample_id": "sample_id", "taxon": "taxon",
        "tax_name": "NCBI_name", "taxid": "NCBI_taxid", "tax_match": "NCBI_match",
        "lineage": "NCBI_lineage",
        "worms_name": "WoRMS_name", "aphia_id": "WoRMS_aphia_id",
        "worms_match": "WoRMS_match", "worms_status": "WoRMS_status",
        "worms_lineage": "WoRMS_lineage",
        "uniq_id": "uniq_id",
        "r1_reads": "r1_reads", "r2_reads": "r2_reads", "files_ok": "files_ok",
        "total_size": "seq_data_size_gb",
        "r1_path": "r1_path", "r2_path": "r2_path"}
    export_cols = [c for c in export_map if c in table.columns]
    export_df = table[export_cols].rename(columns=export_map)
    st.download_button("Download table CSV",
                       _csv_safe_df(export_df).to_csv(index=False).encode(),
                       f"{_config(DB_PATH, 'catalog_slug')}_grab_and_go.csv", "text/csv")

    c1, c2 = st.columns(2, vertical_alignment="bottom")
    id_label = c1.selectbox(
        "MitoPilot ID column", list(id_choices),
        help="Which sample field fills MitoPilot's required unique 'ID' column.")
    mp_rows = omito.build_map_rows(table.to_dict("records"), rows,
                                   id_field=id_choices[id_label])
    mp_df = pd.DataFrame(mp_rows, columns=omito.MITOPILOT_COLUMNS)
    # Not _csv_safe_df: MitoPilot reads this with read.csv and needs ID/R1/R2 verbatim.
    c2.download_button("Download MitoPilot map file",
                       mp_df.to_csv(index=False).encode(),
                       "mitopilot_mapfile.csv", "text/csv",
                       help="CSV with ID, R1, R2, Taxon. R1/R2 are paths relative to "
                            "the copy destination (or to the Store seqdata root); give "
                            "MitoPilot DEST as data_path.")
    mp_issues = omito.issues(mp_rows)
    if mp_issues["n_empty_id"] or mp_issues["duplicate_ids"] or mp_issues["n_missing_reads"]:
        msgs = []
        if mp_issues["n_empty_id"]:
            msgs.append(f"{mp_issues['n_empty_id']} sample(s) have an empty {id_label}")
        if mp_issues["duplicate_ids"]:
            shown = ", ".join(mp_issues["duplicate_ids"][:5])
            more = "…" if len(mp_issues["duplicate_ids"]) > 5 else ""
            msgs.append(f"duplicate ID(s): {shown}{more}")
        if mp_issues["n_missing_reads"]:
            msgs.append(f"{mp_issues['n_missing_reads']} sample(s) missing an R1/R2 filename")
        st.warning("MitoPilot needs a unique ID and both reads per sample — "
                   + "; ".join(msgs) + ". Try a different ID column, or fix the catalog.")
    if mp_issues["n_lane_split"]:
        ids = mp_issues["lane_split_ids"]
        shown = ", ".join(ids[:20]) + (", ..." if len(ids) > 20 else "")
        st.warning(f"MitoPilot takes one R1 and one R2 per sample. {len(ids)} sample(s) in "
                   "the cart have more than one read pair (lane-split, or sequenced over "
                   "several runs); only the first pair of each is in the map. Concatenate "
                   f"the files per direction after copying, or run them separately. ({shown})")

    total, n_files, n_unknown = orclone.estimate_size(rows)

    st.divider()
    st.subheader("Copy sequence data (Hydra I/O queue)")
    m1, m2, m3 = st.columns(3)
    m1.metric("Files", n_files)
    m2.metric("Estimated size", orclone.human_size(total))
    m3.metric("Unknown-size files", n_unknown)
    if n_unknown:
        n_gone = sum(1 for r in rows if pd.isna(r.get("size_bytes")) and r.get("missing_on_disk"))
        why = [f"{n} {label}" for n, label in ((n_unknown - n_gone, "never stat'd"),
                                               (n_gone, "missing on disk")) if n]
        st.caption(f"{n_unknown} file(s) have no recorded size ({', '.join(why)}); "
                   "the estimate excludes them.")

    dest = st.text_input("Destination (directory or rclone remote:path)",
                         placeholder="/pool/public/genomics/<user>/selected_data")
    a1, a2 = st.columns(2)
    # lTIO caps a user at 6 slots; the job requests min(transfers, 6) slots so it
    # always schedules, while rclone still uses `transfers` concurrent transfers.
    transfers = a1.slider("Parallel transfers", 1, 6, 4,
                          help="rclone --transfers (also the mthread slots; lTIO caps 6/user).")
    mem = a2.slider("Memory per slot (GB)", 1, 8, 4, help="lTIO caps 8 GB/slot.")

    groups, n_unres = orclone.group_by_root(rows)
    if n_unres:
        st.caption(f"{n_unres} file(s) can't be copied (no known source root/path) "
                   "and are omitted from the script.")

    if not dest.strip():
        st.info("Enter a destination above to generate the copy script.")
        return

    stamp = orclone.timestamp()
    kw = dict(dest=dest.strip(), transfers=transfers, slots=transfers, mem=mem,
              est_bytes=total, n_files=n_files,
              rclone_module=_config(DB_PATH, "rclone_module"),
              io_queue=_config(DB_PATH, "io_queue"), stamp=stamp)
    script = orclone.build_copy_script(groups, **kw)
    n_copy = sum(len(rels) for _, rels in groups)
    st.caption("Download the script to a `.job` file on Hydra and submit with "
               "`qsub <file>.job` from the login node (lTIO: 6 slots/user, 2 concurrent).")
    with st.expander(f"Script preview ({n_copy} files total; full list in the download)"):
        if n_copy > _SCRIPT_PREVIEW_FILES:
            st.caption(f"Showing the first {_SCRIPT_PREVIEW_FILES} of {n_copy} files.")
        st.code(orclone.build_copy_script(
            orclone.head_groups(groups, _SCRIPT_PREVIEW_FILES), **kw), language="bash")
    st.download_button("Download submission script", script.encode(),
                       f"seqledger_rclone_copy_{stamp}.job", "text/x-shellscript")


def _catalog_caption(db_path, mtime):
    """Identity/staleness banner: which catalog file this is and when it was last
    written. Lets a user reading a synced Scratch copy notice a stale timestamp."""
    when = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    return f"Catalog: {os.path.abspath(db_path)} — last updated {when}"


def main():
    name = _config(DB_PATH, "catalog_name") if os.path.exists(DB_PATH) else "Sequence data catalog"
    st.set_page_config(page_title=name, layout="wide")
    st.title(name)

    if not os.path.exists(DB_PATH):
        st.error(f"Catalog database not found: {DB_PATH}")
        return

    mtime = os.path.getmtime(DB_PATH)
    st.caption(_catalog_caption(DB_PATH, mtime))

    try:
        missing = _missing_schema(DB_PATH)
        if missing:
            st.error("This catalog copy is older than this GUI (missing: "
                     f"{', '.join(missing)}). Ask your data manager to run "
                     "'seqledger init-db' on the master catalog and re-sync this copy.")
            st.stop()
        view_name = st.sidebar.radio(
            "View", ["Projects", "Samples", "Files", "Taxonomy", "Grab & Go"])
        if view_name == "Samples":
            samples_view(load_samples(DB_PATH, mtime), load_files(DB_PATH, mtime))
        elif view_name == "Projects":
            projects_view(load_projects(DB_PATH, mtime),
                          load_data_issues(DB_PATH, mtime),
                          load_integrity_issues(DB_PATH, mtime),
                          load_md5_issues(DB_PATH, mtime))
        elif view_name == "Files":
            files_view(load_files(DB_PATH, mtime), load_samples(DB_PATH, mtime))
        elif view_name == "Taxonomy":
            taxonomy_view(DB_PATH, mtime)
        else:
            custom_table_view(load_samples(DB_PATH, mtime), load_file_paths(DB_PATH, mtime))
    except sqlite3.OperationalError as e:
        if "locked" not in str(e).lower() and "busy" not in str(e).lower():
            raise
        st.warning("Catalog is being updated, retry shortly.")
        st.stop()


if __name__ == "__main__":
    main()

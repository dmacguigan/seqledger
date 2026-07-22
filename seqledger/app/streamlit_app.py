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

NCBI_TAX_URL = "https://www.ncbi.nlm.nih.gov/datasets/taxonomy/"
RANK_COLS = ["tax_domain", "tax_kingdom", "tax_phylum", "tax_class",
             "tax_order", "tax_family", "tax_genus", "tax_species"]
RANK_LABELS = ["domain", "kingdom", "phylum", "class",
               "order", "family", "genus", "species"]

# taxa.match_type ordered best -> worst, with an accessible good->uncertain color
# ramp (works on light + dark). Samples with no taxa row fall into 'unresolved'.
MATCH_ORDER = ["confirmed", "exact", "fuzzy_species", "fuzzy_genus",
               "fuzzy_higher", "unresolved"]
MATCH_LABELS = {"confirmed": "confirmed", "exact": "exact",
                "fuzzy_species": "fuzzy (species)", "fuzzy_genus": "fuzzy (genus)",
                "fuzzy_higher": "fuzzy (higher)", "unresolved": "unresolved"}
MATCH_COLORS = {"confirmed": "#0B7268", "exact": "#3C8A57",
                "fuzzy_species": "#C9A227", "fuzzy_genus": "#C77D11",
                "fuzzy_higher": "#B5532A", "unresolved": "#7A8A86"}

# WoRMS (World Register of Marine Species) resolution, populated by
# `taxonomy resolve --source worms`. WoRMS's top rank is Kingdom (no domain), so its
# lineage is one level shorter than the NCBI one.
WORMS_TAX_URL = "https://www.marinespecies.org/aphia.php?p=taxdetails&id="
WORMS_RANK_COLS = ["worms_kingdom", "worms_phylum", "worms_class", "worms_order",
                   "worms_family", "worms_genus", "worms_species"]
WORMS_RANK_LABELS = ["kingdom", "phylum", "class", "order",
                     "family", "genus", "species"]
# WoRMS TAXAMATCH match types, best -> worst.
WORMS_MATCH_ORDER = ["confirmed", "exact", "phonetic", "near_1", "near_2", "near_3",
                     "match_quarantine", "unresolved"]
WORMS_MATCH_LABELS = {"confirmed": "confirmed", "exact": "exact",
                      "phonetic": "phonetic (sounds-like)", "near_1": "near (1 edit)",
                      "near_2": "near (2 edits)", "near_3": "near (3 edits)",
                      "match_quarantine": "quarantined", "unresolved": "unresolved"}
WORMS_MATCH_COLORS = {"confirmed": "#0B7268", "exact": "#3C8A57",
                      "phonetic": "#C9A227", "near_1": "#C9A227", "near_2": "#C77D11",
                      "near_3": "#B5532A", "match_quarantine": "#B5532A",
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

# data_check_issues.kind -> (short label, one-line explanation). Legacy rows
# ('missing'/'orphan', written before the kind rename) map to the same text so
# older catalogs still read clearly without a re-validate.
_ISSUE_LABEL = {
    "missing from disk":    "missing from disk",
    "missing":              "missing from disk",
    "missing from mapfile": "missing from mapfile",
    "orphan":               "missing from mapfile",
    # Integrity-check failures (files.integrity_status / n_reads), surfaced in the
    # same per-project issues table as the disk/mapfile checks.
    "gzip error":           "gzip error",
    "format error":         "format error",
    "empty (0 reads)":      "empty (0 reads)",
}
_ISSUE_DETAIL = {
    "missing from disk":    "Sequence file is listed in the mapfile but was not found on disk.",
    "missing":              "Sequence file is listed in the mapfile but was not found on disk.",
    "missing from mapfile": "Sequence file is present on disk but is not referenced by any mapfile row.",
    "orphan":               "Sequence file is present on disk but is not referenced by any mapfile row.",
    "gzip error":           "File is not valid gzip or is truncated (failed decompression / `gunzip -t`).",
    "format error":         "File decompresses but is not valid FASTQ (line count not a multiple of 4).",
    "empty (0 reads)":      "File passed the gzip/FASTQ check but contains 0 reads (empty file or failed transfer?).",
}

# projects.metadata_status -> (short label, plain-english meaning). The full
# per-project sentence is stored in metadata_detail; this is the at-a-glance label.
_MAPFILE_LABEL = {
    "ok":              "OK",
    "unknown":         "unknown",
    "missing_mapfile": "no mapfile",
    "missing_seqdata": "no data folder",
    "broken_mapfile":  "broken mapfile",
    "invalid_mapfile": "no usable rows",
    "flagged":         "flagged rows",
}
_MAPFILE_DETAIL = {
    "unknown": "This catalog copy predates the mapfile-status check, or the status "
               "isn't set. Re-ingest (or re-sync the GUI copy) to populate it.",
    "missing_mapfile": "A project folder is on disk but has no '<project>_mapfile.csv' "
                       "in the metadata directory. Files were cataloged from disk; "
                       "sample metadata (taxon, UniqID) is missing until a mapfile is added.",
    "missing_seqdata": "A mapfile exists but no matching project folder was found in the "
                       "sequence-data directory. Samples were cataloged from the mapfile, "
                       "but no files are on disk.",
    "broken_mapfile":  "The mapfile is present but its header is malformed (expected "
                       "ID,R1,R2,Taxon,UniqID). Files were cataloged from disk; sample "
                       "metadata was skipped until the mapfile is fixed.",
    "invalid_mapfile": "The mapfile header is valid but no rows could be loaded (all had "
                       "empty or duplicate IDs). Files were cataloged from disk. Fix the "
                       "mapfile and re-ingest.",
    "flagged":         "The mapfile loaded, but some rows needed repair: empty Taxon/UniqID "
                       "were filled with 'NA', and empty-ID or duplicate-ID rows were "
                       "skipped. The affected samples are marked in the 'flags' column. Fix "
                       "the mapfile and re-ingest to clear them.",
}


def _ro_connect(db_path):
    """Open the catalog read-only (the GUI never writes).

    mode=ro (not immutable) so it stays correct whether the DB is a static Scratch
    replica OR the live master read directly on the I/O queue (`gui --qsub`), where
    the CLI may be writing concurrently -- ro respects locking; immutable would not.
    It also prevents a typo'd path from auto-creating an empty DB file.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _sql(db_path, query):
    con = _ro_connect(db_path)
    try:
        return pd.read_sql_query(query, con)
    finally:
        con.close()


def _config(db_path, key):
    """Per-catalog config value (falls back to seqledger defaults)."""
    try:
        con = _ro_connect(db_path)
        try:
            return odb.get_config(con, key)
        finally:
            con.close()
    except sqlite3.Error:
        return odb.CONFIG_DEFAULTS.get(key)


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
    # Tolerate a catalog copy that predates the samples.flags migration (the GUI
    # opens read-only and never migrates -- e.g. an older synced Scratch copy).
    flags = "s.flags" if "flags" in _table_columns(db_path, "samples") else "NULL"
    # Tolerate a catalog copy predating the WoRMS columns (older read-only replica).
    # worms_url embeds the accepted name in a URL '#'-fragment so the link column can
    # display the name as its label (a LinkColumn display_text regex), exactly like
    # ncbi_url -- no separate name/link columns. worms_name is kept for search + CSV.
    worms_sel = (
        "t.worms_sci_name AS worms_name, t.worms_status AS worms_status, "
        "t.worms_match_type AS worms_match, t.aphia_id AS aphia_id, "
        "t.worms_lineage AS worms_lineage, "
        f"CASE WHEN t.aphia_id IS NOT NULL THEN '{WORMS_TAX_URL}' || t.aphia_id "
        "|| '#' || COALESCE(t.worms_sci_name, s.taxon) END AS worms_url"
        if "aphia_id" in _table_columns(db_path, "taxa")
        else "NULL AS worms_name, NULL AS worms_status, NULL AS worms_match, "
             "NULL AS aphia_id, NULL AS worms_lineage, NULL AS worms_url")
    return _with_uniqid_url(_sql(db_path, f"""
        SELECT s.project_id, s.sample_id, s.taxon, s.uniq_id, {flags} AS flags,
               p.source, p.seq_data_relpath AS data_dir,
               COALESCE(b.verified, 0) AS backup_verified,
               t.sci_name AS tax_name, t.match_type AS tax_match,
               t.taxid AS taxid, t.lineage AS lineage,
               CASE WHEN t.taxid IS NOT NULL
                    THEN '{NCBI_TAX_URL}' || t.taxid || '/#'
                         || COALESCE(t.sci_name, s.taxon) END AS ncbi_url,
               {worms_sel},
               (SELECT {_FULL_PATH} FROM files f
                  WHERE f.project_id = s.project_id AND f.sample_pk = s.sample_pk
                    AND f.direction = 'R1') AS r1_path,
               (SELECT f.owner_name FROM files f
                  WHERE f.project_id = s.project_id AND f.sample_pk = s.sample_pk
                    AND f.direction = 'R1') AS r1_owner,
               (SELECT {_FULL_PATH} FROM files f
                  WHERE f.project_id = s.project_id AND f.sample_pk = s.sample_pk
                    AND f.direction = 'R2') AS r2_path,
               (SELECT f.owner_name FROM files f
                  WHERE f.project_id = s.project_id AND f.sample_pk = s.sample_pk
                    AND f.direction = 'R2') AS r2_owner
        FROM samples s
        JOIN projects p ON p.project_id = s.project_id
        LEFT JOIN backups b ON b.project_id = s.project_id AND b.location = 'pdrive'
        LEFT JOIN taxa t ON t.taxon = s.taxon
        ORDER BY s.project_id, s.sample_id"""))


@st.cache_data(ttl=60)
def load_taxonomy(db_path, mtime):
    # Select both the NCBI and WoRMS rank/match columns so the view's source toggle
    # can switch client-side. Guard each column for an older copy that predates the
    # WoRMS migration (missing -> NULL), so the query never hits 'no such column'.
    taxa_cols = _table_columns(db_path, "taxa")
    def col(c):
        return f"t.{c}" if c in taxa_cols else f"NULL AS {c}"
    extra = ", ".join(col(c) for c in RANK_COLS + ["worms_match_type"] + WORMS_RANK_COLS)
    return _sql(db_path, f"""
        SELECT s.project_id, s.sample_id, s.taxon, t.taxid, t.match_type,
               {extra}
        FROM samples s
        LEFT JOIN taxa t ON t.taxon = s.taxon
        ORDER BY s.project_id, s.sample_id""")


def _table_columns(db_path, table):
    con = _ro_connect(db_path)
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


@st.cache_data(ttl=60)
def load_projects(db_path, mtime):
    # Tolerate catalogs created before the metadata_status/detail migration: the
    # GUI opens the DB read-only and never migrates, so fall back to defaults when
    # those columns are absent (older synced copy on Scratch, etc.).
    # Missing status coalesces to 'unknown', NOT 'ok': turning "we don't know" into
    # "healthy" would hide real mapfile problems on an old/unmigrated copy.
    cols = _table_columns(db_path, "projects")
    mstatus = ("COALESCE(p.metadata_status, 'unknown')"
               if "metadata_status" in cols else "'unknown'")
    mdetail = "p.metadata_detail" if "metadata_detail" in cols else "NULL"
    return _sql(db_path, f"""
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
               (SELECT MAX(f.integrity_date) FROM files f
                  WHERE f.project_id = p.project_id) AS integrity_date,
               p.owner_name, p.seq_data_relpath AS data_dir,
               {mstatus} AS metadata_status,
               {mdetail} AS metadata_detail,
               p.data_check_date, p.date_ingested
        FROM projects p
        ORDER BY p.project_id""")


@st.cache_data(ttl=60)
def load_data_issues(db_path, mtime):
    return _with_uniqid_url(_sql(db_path, f"""
        SELECT i.project_id, i.kind, i.filename,
               {_FULL_PATH} AS full_path,
               s.sample_id, f.direction, s.taxon, s.uniq_id
        FROM data_check_issues i
        LEFT JOIN files f ON f.project_id = i.project_id AND f.filename = i.filename
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
               f.filename, {_FULL_PATH} AS full_path,
               s.sample_id, f.direction, s.taxon, s.uniq_id
        FROM files f
        LEFT JOIN projects p ON p.project_id = f.project_id
        LEFT JOIN samples s ON s.sample_pk = f.sample_pk
        WHERE f.integrity_status IN ('gzip_error', 'format_error')
           OR (f.integrity_status = 'ok' AND f.n_reads = 0)
        ORDER BY f.project_id, kind, f.filename"""))


def backup_label(v):
    """Plain-english P-drive backup state from the 1/0 backups.verified flag."""
    return "Verified" if v == 1 else "Not verified"


def data_files_label(row):
    status = row["data_check_status"]
    if status == "ok":
        return "OK"
    if status != "issues":
        return "unchecked"
    parts = []
    if row["data_check_n_missing"]:
        parts.append(f"{int(row['data_check_n_missing'])} missing from disk")
    if row["data_check_n_orphan"]:
        parts.append(f"{int(row['data_check_n_orphan'])} missing from mapfile")
    return ", ".join(parts) or "issues"


def checksum_label(row):
    if row["n_files"] == 0:
        return "no files"
    if row["n_mismatch"]:
        return f"{int(row['n_mismatch'])} mismatch"
    if row["n_uncompared"]:
        return f"incomplete ({int(row['n_uncompared'])} uncompared)"
    return "verified"


def mapfile_label(row):
    return _MAPFILE_LABEL.get(row["metadata_status"], row["metadata_status"])


def integrity_label(row):
    n = row["n_files"]
    if n == 0:
        return "no files"
    ok = int(row["n_integrity_ok"] or 0)
    bad = int(row["n_integrity_bad"] or 0)
    if bad:
        return f"{bad} corrupt"
    if ok == 0:
        return "unchecked"
    rest = n - ok - bad
    if rest:
        return f"incomplete ({rest} unchecked)"
    return "verified"


@st.cache_data(ttl=60)
def load_files(db_path, mtime):
    return _sql(db_path, f"""
        SELECT f.project_id, s.sample_id, f.direction, f.filename,
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
               f.size_bytes, f.n_reads
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


def _download(df, name):
    st.download_button("Download CSV", df.to_csv(index=False).encode(), name, "text/csv")


def _column_filters(df, cols, key):
    """Per-column substring filters shown as a row of boxes above a table.

    Streamlit can't put inputs inside a dataframe header, so this renders a row of
    small text boxes (one per column, column name as placeholder) in a collapsible
    'Filter columns' bar just above the table. Returns df with rows kept only where
    every non-empty box's text appears (case-insensitive) in that column. All
    columns are preserved, so downstream row selection still works.
    """
    with st.expander("🔍 Filter columns", expanded=False):
        boxes = st.columns(len(cols))
        active = {}
        for box, c in zip(boxes, cols):
            val = box.text_input(c, key=f"colf_{key}_{c}", placeholder=c,
                                 label_visibility="collapsed")
            if val.strip():
                active[c] = val.strip()
    out = df
    for c, val in active.items():
        out = out[out[c].fillna("").astype(str).str.contains(val, case=False, na=False, regex=False)]
    return out


def samples_view(df, files_df):
    with st.sidebar:
        st.header("Filters")
        projects = sorted(df["project_id"].unique())
        chosen = st.multiselect("Project", projects)
        backup = st.selectbox("Backup status", ["All", "Verified", "Not verified"])
        search = st.text_input("Search (sample, taxon, UniqID)")

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
    on_screen = ["project_id", "sample_id", "taxon",
                 "ncbi_url", "tax_match", "worms_url", "worms_match", "worms_status",
                 "uniq_id", "uniq_id_url", "flags", "backup"]
    show = _column_filters(show, on_screen, "samples")
    st.caption(f"{len(show)} of {len(df)} samples (full R1/R2 paths, taxid + "
               "lineage are in the CSV export). Select a row to list its files below.")
    event = st.dataframe(
        show[on_screen], width="stretch", hide_index=True,
        on_select="rerun", selection_mode="single-row", key="samples_table",
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
    _download(show, f"{_config(DB_PATH, 'catalog_slug')}_samples.csv")

    sel = event.selection.rows if event and event.selection else []
    if not sel:
        return
    r = show.iloc[sel[0]]
    pid, sid = r["project_id"], r["sample_id"]
    fsub = files_df[(files_df["project_id"] == pid)
                    & (files_df["sample_id"] == sid)].copy()
    st.subheader(f"Files for sample {sid} ({pid})")
    if len(fsub):
        fsub["size"] = fsub["size_bytes"].map(human_size)
        st.dataframe(
            fsub[["direction", "filename", "full_path", "size", "owner_name",
                  "backup", "integrity", "n_reads", "integrity_date"]],
            width="stretch", hide_index=True,
            column_config={"n_reads": "reads", "integrity_date": "checked"})
        _download(fsub, f"{sid}_files.csv")
    else:
        st.info("No files cataloged for this sample.")


def projects_view(df, issues, integ_issues):
    with st.sidebar:
        st.header("Filters")
        search = st.text_input("Search (project, description)")
        mapfile_only = st.checkbox("Only mapfile issues")
        data_only = st.checkbox("Only data-files issues")
        cs_only = st.checkbox("Only checksum issues")
        integ_only = st.checkbox("Only integrity issues")
    view = df
    if search:
        s = search.lower()
        mask = (_contains(view["project_id"], s) | _contains(view["description"], s))
        view = view[mask]
    if mapfile_only:
        view = view[view["metadata_status"] != "ok"]
    if data_only:
        view = view[view["data_check_status"] == "issues"]
    if cs_only:
        view = view[(view["n_mismatch"] > 0) | (view["n_uncompared"] > 0)]
    if integ_only:
        view = view[view["n_integrity_bad"] > 0]

    show = view.copy()
    show["mapfile"] = show.apply(mapfile_label, axis=1) if len(show) else []
    show["data_files"] = show.apply(data_files_label, axis=1) if len(show) else []
    show["checksum"] = show.apply(checksum_label, axis=1) if len(show) else []
    show["integrity"] = show.apply(integrity_label, axis=1) if len(show) else []
    cols = ["project_id", "source", "description", "n_samples", "n_files",
            "mapfile", "data_files", "checksum", "integrity", "owner_name", "data_dir",
            "date_ingested"]
    show = _column_filters(show, cols, "projects")
    st.caption(f"{len(show)} of {len(df)} projects. 'mapfile' flags a folder with no "
               "mapfile, a mapfile with no folder, or a broken mapfile. Select a row for "
               "its mapfile explanation + data-files & integrity issues below.")
    event = st.dataframe(show[cols], width="stretch", hide_index=True,
                         on_select="rerun", selection_mode="single-row",
                         key="projects_table")
    _download(show, f"{_config(DB_PATH, 'catalog_slug')}_projects.csv")

    sel = event.selection.rows if event and event.selection else []
    if sel:
        prow = show.iloc[sel[0]]
        pid = prow["project_id"]
        mstatus = prow["metadata_status"]
        if mstatus != "ok":
            detail = prow.get("metadata_detail") or _MAPFILE_DETAIL.get(mstatus, "")
            st.warning(f"**Mapfile issue ({_MAPFILE_LABEL.get(mstatus, mstatus)}):** {detail}")
        # Disk/mapfile checks (data_check_issues) and integrity-check failures live in
        # different tables but are the same question to a user ("what's wrong with this
        # project's files?"), so surface both here instead of hiding integrity errors
        # behind the Files view. Both frames share columns, so concat + one kind->label
        # map renders them together.
        sub = pd.concat([issues[issues["project_id"] == pid],
                         integ_issues[integ_issues["project_id"] == pid]],
                        ignore_index=True).sort_values(
            ["kind", "filename"], kind="stable")
        st.subheader(f"Data-files issues: {pid}")
        if len(sub):
            sub = sub.copy()
            sub["issue"] = sub["kind"].map(_ISSUE_LABEL).fillna(sub["kind"])
            sub["detail"] = sub["kind"].map(_ISSUE_DETAIL).fillna("")
            st.dataframe(
                sub[["issue", "detail", "filename", "full_path", "sample_id",
                     "direction", "taxon", "uniq_id", "uniq_id_url"]],
                width="stretch", hide_index=True,
                column_config={"full_path": "expected_path",
                               "uniq_id_url": _uniqid_link_col()})
            _download(sub, f"{pid}_data_issues.csv")
        else:
            st.info("No recorded data-files or integrity issues. Run "
                    "`validate --seqdata-root` and `integrity --collect` to refresh.")


def files_view(df, samples_df):
    with st.sidebar:
        st.header("Filters")
        projects = sorted(df["project_id"].unique())
        chosen = st.multiselect("Project", projects)
        backup = st.selectbox("Backup status", ["All", "Verified", "Mismatch", "Not compared"])
        integrity = st.selectbox(
            "Integrity", ["All", "ok", "gzip_error", "format_error", "unchecked"])
        search = st.text_input("Search (sample, filename)")
    view = df
    if chosen:
        view = view[view["project_id"].isin(chosen)]
    if backup != "All":
        view = view[view["backup"] == backup]
    if integrity != "All":
        view = view[view["integrity"] == integrity]
    if search:
        s = search.lower()
        mask = (_contains(view["sample_id"], s) | _contains(view["filename"], s))
        view = view[mask]
    show = view.copy()
    show["size"] = show["size_bytes"].map(human_size)
    on_screen = ["project_id", "sample_id", "direction", "filename", "full_path",
                 "size", "owner_name", "backup", "integrity", "n_reads",
                 "integrity_date"]
    show = _column_filters(show, on_screen, "files")
    st.caption(f"{len(show)} of {len(df)} files. "
               "Select a row to show its sample info below.")
    event = st.dataframe(
        show[on_screen], width="stretch", hide_index=True,
        on_select="rerun", selection_mode="single-row", key="files_table",
        column_config={"n_reads": "reads", "integrity_date": "checked"})
    _download(show, f"{_config(DB_PATH, 'catalog_slug')}_files.csv")

    sel = event.selection.rows if event and event.selection else []
    if not sel:
        return
    r = show.iloc[sel[0]]
    pid, sid = r["project_id"], r["sample_id"]
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
def _matchtype_counts(db_path, mtime, source, chosen):
    """Sample counts by taxonomy match_type (resolution confidence)."""
    df = load_taxonomy(db_path, mtime)
    s = SOURCES[source]
    v = df if not chosen else df[df["project_id"].isin(list(chosen))]
    mt = v[s["match_col"]].replace("", pd.NA).fillna("unresolved")
    counts = mt.value_counts().rename_axis("match_type").reset_index(name="samples")
    counts["order"] = counts["match_type"].map(
        {m: i for i, m in enumerate(s["match_order"])}).fillna(len(s["match_order"]))
    counts["label"] = counts["match_type"].map(s["match_labels"]).fillna(counts["match_type"])
    return counts.sort_values("order")


@st.cache_data(ttl=60)
def _composition_counts(db_path, mtime, chosen, rank_col):
    """Per-project sample counts within one rank (for the stacked composition bar)."""
    df = load_taxonomy(db_path, mtime)
    v = df if not chosen else df[df["project_id"].isin(list(chosen))]
    v = v[["project_id", rank_col]].copy()
    v[rank_col] = v[rank_col].replace("", pd.NA).fillna("unknown")
    return v.groupby(["project_id", rank_col], sort=False).size().reset_index(name="samples")


def taxonomy_view(db_path, mtime):
    import plotly.express as px

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
    mt = _matchtype_counts(db_path, mtime, source, tuple(chosen))
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
    comp = _composition_counts(db_path, mtime, tuple(chosen), rank_col)
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


def _cart_key(project_id, sample_id):
    return f"{project_id}\t{sample_id}"


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

    st.subheader("Search results")
    st.caption(f"{len(view)} match(es). Select rows and click **Add to custom table**. "
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

    tcols = ["project_id", "sample_id", "taxon",
             "ncbi_url", "taxid", "tax_match",
             "worms_url", "worms_match", "worms_status",
             "total_size", "r1_reads", "r2_reads",
             "uniq_id", "uniq_id_url", "r1_path", "r2_path"]
    tcols = [c for c in tcols if c in table.columns]
    tevent = st.dataframe(
        table[tcols], width="stretch", hide_index=True,
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
    # Files backing the selected samples: paths + sizes for rclone, R1/R2
    # basenames for the MitoPilot map file.
    fpaths = paths_df[paths_df.set_index(["project_id", "sample_id"]).index.isin(keyset)]
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
        "r1_reads": "r1_reads", "r2_reads": "r2_reads",
        "total_size": "seq_data_size_gb",
        "r1_path": "r1_path", "r2_path": "r2_path"}
    export_cols = [c for c in export_map if c in table.columns]
    export_df = table[export_cols].rename(columns=export_map)
    st.download_button("Download table CSV",
                       export_df.to_csv(index=False).encode(),
                       f"{_config(DB_PATH, 'catalog_slug')}_grab_and_go.csv", "text/csv")

    c1, c2 = st.columns(2, vertical_alignment="bottom")
    id_label = c1.selectbox(
        "MitoPilot ID column", list(id_choices),
        help="Which sample field fills MitoPilot's required unique 'ID' column.")
    mp_rows = omito.build_map_rows(table.to_dict("records"), rows,
                                   id_field=id_choices[id_label])
    mp_df = pd.DataFrame(mp_rows, columns=omito.MITOPILOT_COLUMNS)
    c2.download_button("Download MitoPilot map file",
                       mp_df.to_csv(index=False).encode(),
                       "mitopilot_mapfile.csv", "text/csv",
                       help="CSV with ID, R1, R2, Taxon (R1/R2 are filenames; give "
                            "MitoPilot the copied data dir as its data_path).")
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

    total, n_files, n_unknown = orclone.estimate_size(rows)

    st.divider()
    st.subheader("Copy sequence data (Hydra I/O queue)")
    m1, m2, m3 = st.columns(3)
    m1.metric("Files", n_files)
    m2.metric("Estimated size", orclone.human_size(total))
    m3.metric("Unknown-size files", n_unknown)
    if n_unknown:
        st.caption(f"{n_unknown} file(s) have no recorded size (never stat'd on disk); "
                   "the estimate excludes them, so the real total is larger.")

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

    script = orclone.build_copy_script(
        groups, dest.strip(), transfers=transfers, slots=transfers, mem=mem,
        est_bytes=total, n_files=n_files,
        rclone_module=_config(DB_PATH, "rclone_module"),
        io_queue=_config(DB_PATH, "io_queue"))
    st.caption("Copy this into a `.job` file on Hydra and submit with "
               "`qsub <file>.job` from the login node (lTIO: 6 slots/user, 2 concurrent).")
    st.code(script, language="bash")
    st.download_button("Download submission script", script.encode(),
                       "seqledger_rclone_copy.job", "text/x-shellscript")


def main():
    name = _config(DB_PATH, "catalog_name") if os.path.exists(DB_PATH) else "Sequence data catalog"
    st.set_page_config(page_title=name, layout="wide")
    st.title(name)

    if not os.path.exists(DB_PATH):
        st.error(f"Catalog database not found: {DB_PATH}")
        return

    view_name = st.sidebar.radio(
        "View", ["Projects", "Samples", "Files", "Taxonomy", "Grab & Go"])
    mtime = os.path.getmtime(DB_PATH)
    if view_name == "Samples":
        samples_view(load_samples(DB_PATH, mtime), load_files(DB_PATH, mtime))
    elif view_name == "Projects":
        projects_view(load_projects(DB_PATH, mtime),
                      load_data_issues(DB_PATH, mtime),
                      load_integrity_issues(DB_PATH, mtime))
    elif view_name == "Files":
        files_view(load_files(DB_PATH, mtime), load_samples(DB_PATH, mtime))
    elif view_name == "Taxonomy":
        taxonomy_view(DB_PATH, mtime)
    else:
        custom_table_view(load_samples(DB_PATH, mtime), load_file_paths(DB_PATH, mtime))


if __name__ == "__main__":
    main()

"""Read-only Streamlit GUI for browsing the Ocean DNA sequence data catalog.

Launch via `seqledger gui --db PATH` (which sets SEQLEDGER_DB and prints the SSH
tunnel command). No SQL knowledge required: pick a view, search, filter, download CSV.

Views:
  Projects     one row per sequencing project, with summary stats + owner
  Samples      one row per sample (CSV export carries full R1/R2 paths + owner)
  Files        one row per FASTQ, full absolute path, size, owner, backup status
  Taxonomy     interactive breadth of NCBI-resolved sample taxonomy
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
}
_ISSUE_DETAIL = {
    "missing from disk":    "Sequence file is listed in the mapfile but was not found on disk.",
    "missing":              "Sequence file is listed in the mapfile but was not found on disk.",
    "missing from mapfile": "Sequence file is present on disk but is not referenced by any mapfile row.",
    "orphan":               "Sequence file is present on disk but is not referenced by any mapfile row.",
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
    return st.column_config.LinkColumn("UniqID link", display_text="open ↗")


@st.cache_data(ttl=60)
def load_samples(db_path, mtime):
    # Tolerate a catalog copy that predates the samples.flags migration (the GUI
    # opens read-only and never migrates -- e.g. an older synced Scratch copy).
    flags = "s.flags" if "flags" in _table_columns(db_path, "samples") else "NULL"
    return _with_uniqid_url(_sql(db_path, f"""
        SELECT s.project_id, s.sample_id, s.taxon, s.uniq_id, {flags} AS flags,
               p.source, p.seq_data_relpath AS data_dir,
               COALESCE(b.verified, 0) AS backup_verified,
               t.sci_name AS tax_name, t.match_type AS tax_match,
               t.taxid AS taxid, t.lineage AS lineage,
               CASE WHEN t.taxid IS NOT NULL
                    THEN '{NCBI_TAX_URL}' || t.taxid || '/#'
                         || COALESCE(t.sci_name, s.taxon) END AS ncbi_url,
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
    return _sql(db_path, f"""
        SELECT s.project_id, s.sample_id, s.taxon, t.taxid, t.match_type,
               {', '.join('t.' + c for c in RANK_COLS)}
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
               p.seqdata_root, f.rel_path, {_FULL_PATH} AS full_path, f.size_bytes
        FROM files f
        JOIN projects p ON p.project_id = f.project_id
        LEFT JOIN samples s ON s.sample_pk = f.sample_pk
        ORDER BY f.project_id, s.sample_id, f.direction""")


def human_size(n):
    if n is None or pd.isna(n):
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _contains(series, s):
    """Case-insensitive substring mask, safe on all-null columns.

    A column that is entirely NULL loads as float64, where `.str` raises. fillna +
    astype(str) normalizes to text first so search never crashes a view.
    """
    return series.fillna("").astype(str).str.lower().str.contains(s, na=False)


def _download(df, name):
    st.download_button("Download CSV", df.to_csv(index=False).encode(), name, "text/csv")


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
                | _contains(view["uniq_id"], s))
        view = view[mask]

    st.caption(f"{len(view)} of {len(df)} samples (full R1/R2 paths, taxid + "
               "lineage are in the CSV export). Select a row to list its files below.")
    show = view.copy()
    show["backup"] = show["backup_verified"].map(backup_label)
    on_screen = ["project_id", "sample_id", "taxon", "ncbi_url", "tax_match",
                 "uniq_id", "uniq_id_url", "flags", "backup"]
    event = st.dataframe(
        show[on_screen], width="stretch", hide_index=True,
        on_select="rerun", selection_mode="single-row", key="samples_table",
        column_config={
            "tax_match": "match type",
            # The matched name (carried in the URL fragment) is the link label,
            # linking to its NCBI datasets taxonomy page.
            "ncbi_url": st.column_config.LinkColumn(
                "NCBI taxon match", display_text=r"#(.+)$"),
            "uniq_id_url": _uniqid_link_col()})
    _download(show, f"{_config(DB_PATH, 'catalog_slug')}_samples.csv")

    sel = event.selection.rows if event and event.selection else []
    if not sel:
        return
    r = view.iloc[sel[0]]
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


def projects_view(df, issues):
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
    st.caption(f"{len(view)} of {len(df)} projects. 'mapfile' flags a folder with no "
               "mapfile, a mapfile with no folder, or a broken mapfile. Select a row for "
               "its mapfile explanation + data-files issues below.")
    cols = ["project_id", "source", "description", "n_samples", "n_files",
            "mapfile", "data_files", "checksum", "integrity", "owner_name", "data_dir",
            "date_ingested"]
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
        sub = issues[issues["project_id"] == pid].copy()
        st.subheader(f"Data-files issues: {pid}")
        if len(sub):
            sub["issue"] = sub["kind"].map(_ISSUE_LABEL).fillna(sub["kind"])
            sub["detail"] = sub["kind"].map(_ISSUE_DETAIL).fillna("")
            st.dataframe(
                sub[["issue", "detail", "filename", "full_path", "sample_id",
                     "direction", "taxon", "uniq_id", "uniq_id_url"]],
                width="stretch", hide_index=True,
                column_config={"full_path": "expected path",
                               "uniq_id_url": _uniqid_link_col()})
            _download(sub, f"{pid}_data_issues.csv")
        else:
            st.info("No recorded data-files issues. "
                    "Run `validate --seqdata-root` to refresh.")


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
    st.caption(f"{len(view)} of {len(df)} files. "
               "Select a row to show its sample info below.")
    show = view.copy()
    show["size"] = show["size_bytes"].map(human_size)
    on_screen = ["project_id", "sample_id", "direction", "filename", "full_path",
                 "size", "owner_name", "backup", "integrity", "n_reads",
                 "integrity_date"]
    event = st.dataframe(
        show[on_screen], width="stretch", hide_index=True,
        on_select="rerun", selection_mode="single-row", key="files_table",
        column_config={"n_reads": "reads", "integrity_date": "checked"})
    _download(view, f"{_config(DB_PATH, 'catalog_slug')}_files.csv")

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
                            "taxid", "lineage", "uniq_id", "uniq_id_url",
                            "backup", "r1_path", "r2_path"] if c in ssub.columns]
        st.dataframe(
            ssub[cols], width="stretch", hide_index=True,
            column_config={
                "tax_match": "match type",
                "ncbi_url": st.column_config.LinkColumn(
                    "NCBI taxon match", display_text=r"#(.+)$"),
                "uniq_id_url": _uniqid_link_col()})
        _download(ssub, f"{sid}_sample.csv")
    else:
        st.info("No sample record for this file "
                "(it may be an orphan not tied to a cataloged sample).")


@st.cache_data(ttl=60)
def _taxonomy_counts(db_path, mtime, chosen, depth, include_unknown):
    """Per-lineage sample counts for the sunburst, cached across reruns.

    Grouping ~6k samples over up to 8 rank columns is the per-interaction cost;
    caching on (projects, depth, include_unknown) keeps filter/slider changes
    from re-grouping every rerun. Returns (counts_df, n_samples, n_projects).
    """
    df = load_taxonomy(db_path, mtime)
    cols = RANK_COLS[:depth]
    v = df if not chosen else df[df["project_id"].isin(list(chosen))]
    v = v[["project_id"] + cols].copy()
    for c in cols:
        v[c] = v[c].replace("", pd.NA).fillna("unknown")
    if not include_unknown:
        v = v[v[cols[-1]] != "unknown"]
    counts = v.groupby(cols, sort=False).size().reset_index(name="samples")
    return counts, len(v), v["project_id"].nunique()


@st.cache_data(ttl=60)
def _matchtype_counts(db_path, mtime, chosen):
    """Sample counts by taxonomy match_type (resolution confidence)."""
    df = load_taxonomy(db_path, mtime)
    v = df if not chosen else df[df["project_id"].isin(list(chosen))]
    mt = v["match_type"].replace("", pd.NA).fillna("unresolved")
    counts = mt.value_counts().rename_axis("match_type").reset_index(name="samples")
    counts["order"] = counts["match_type"].map(
        {m: i for i, m in enumerate(MATCH_ORDER)}).fillna(len(MATCH_ORDER))
    counts["label"] = counts["match_type"].map(MATCH_LABELS).fillna(counts["match_type"])
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
        projects = sorted(df["project_id"].unique())
        chosen = st.multiselect("Project", projects)
        chart_type = st.radio("Hierarchy chart", ["Sunburst", "Treemap"], horizontal=True)
        depth_label = st.selectbox("Deepest rank", RANK_LABELS,
                                   index=RANK_LABELS.index("order"))
        include_unknown = st.checkbox("Include 'unknown'", value=True)
        ring_cap = st.slider(
            "Levels shown", 2, len(RANK_COLS), 4,
            help="Rank levels drawn up front. Sunburst: click a wedge to drill deeper. "
                 "Treemap: raise this to expand (fewer = faster). Clicking scopes the bar below.")
        comp_label = st.selectbox("Composition rank (stacked bar)", RANK_LABELS,
                                  index=RANK_LABELS.index("phylum"))

    depth = RANK_LABELS.index(depth_label) + 1
    cols = RANK_COLS[:depth]
    counts, n_samples, n_proj = _taxonomy_counts(
        db_path, mtime, tuple(chosen), depth, include_unknown)

    st.caption(f"{n_samples} samples across {n_proj} project(s); "
               "run `taxonomy resolve` to (re)populate. Unranked -> 'unknown'.")
    if not n_samples:
        st.info("No resolved taxonomy yet. Run `seqledger taxonomy resolve`.")
        return

    # --- Hierarchy chart: sunburst or treemap ---
    # px.sunburst/treemap send + lay out EVERY node in `path`, so passing the full
    # species-deep tree is thousands of nodes. The two charts pay for that
    # differently, so they're built differently:
    #  - Sunburst: cheap radial arcs. Pre-load the full depth and draw `ring_cap`
    #    rings up front (maxdepth); clicking a wedge drills deeper client-side with
    #    no rerun. This is fast enough even deep.
    #  - Treemap: squarified layout + a labeled rectangle per tile is costly over
    #    thousands of tiles (the reported slowness). Build it from only the shown
    #    levels so the node count stays small; the "Levels shown" slider expands it.
    st.subheader(f"Taxonomic breadth ({chart_type.lower()})")
    if chart_type == "Treemap":
        chart_cols = cols[:min(ring_cap, depth)]
        chart_counts = (counts.groupby(chart_cols, sort=False)["samples"].sum()
                        .reset_index())
        if len(chart_counts) > 1500:
            st.warning(f"{len(chart_counts):,} tiles at this depth — the treemap may be "
                       "slow. Lower 'Levels shown', pick a coarser 'Deepest rank', filter "
                       "by project, or switch to Sunburst (it drills deeper on click).")
        fig = px.treemap(chart_counts, path=chart_cols, values="samples")
    else:
        chart_cols, chart_counts = cols, counts
        fig = px.sunburst(counts, path=cols, values="samples")
        fig.update_traces(maxdepth=min(ring_cap, depth))  # draw ring_cap rings; drill deeper on click
    fig.update_layout(margin=dict(t=10, l=10, r=10, b=10))
    event = st.plotly_chart(fig, width="stretch", on_select="rerun", key="hierarchy")

    # Clicking a node scopes the detail bar to that node's subtree. Both px.sunburst
    # and px.treemap build each node id as the "/"-joined lineage.
    lineage = _selected_lineage(event, chart_counts, chart_cols)
    sub = counts
    for col, val in zip(chart_cols, lineage):
        sub = sub[sub[col] == val]

    if lineage:
        st.caption("Bar chart scoped to: " + " › ".join(lineage)
                   + "  — click the chart's center/root to reset.")
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
    st.caption("How each sample's taxon resolved against NCBI: confirmed/exact are "
               "reliable; fuzzy is a best-guess correction; unresolved had no match.")
    mt = _matchtype_counts(db_path, mtime, tuple(chosen))
    fig_mt = px.bar(mt, x="samples", y="label", orientation="h",
                    color="match_type", color_discrete_map=MATCH_COLORS,
                    category_orders={"label": list(mt["label"])[::-1]})
    fig_mt.update_layout(showlegend=False, yaxis_title=None, xaxis_title="samples",
                         margin=dict(t=6, l=6, r=6, b=6), height=max(160, 34 * len(mt)))
    st.plotly_chart(fig_mt, width="stretch", key="matchtype")

    # --- Composition by project: stacked sample counts within a coarse rank ---
    st.divider()
    st.subheader(f"Composition by project ({comp_label})")
    rank_col = RANK_COLS[RANK_LABELS.index(comp_label)]
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
        column_config={"tax_name": "NCBI name", "uniq_id_url": _uniqid_link_col()})

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

    tcols = ["project_id", "sample_id", "taxon", "tax_name", "taxid", "uniq_id",
             "uniq_id_url", "lineage", "r1_path", "r2_path"]
    tcols = [c for c in tcols if c in table.columns]
    tevent = st.dataframe(
        table[tcols], width="stretch", hide_index=True,
        on_select="rerun", selection_mode="multi-row", key="cart_table",
        column_config={"uniq_id_url": _uniqid_link_col()})
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

    st.download_button("Download table CSV",
                       table[tcols].to_csv(index=False).encode(),
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
                      load_data_issues(DB_PATH, mtime))
    elif view_name == "Files":
        files_view(load_files(DB_PATH, mtime), load_samples(DB_PATH, mtime))
    elif view_name == "Taxonomy":
        taxonomy_view(DB_PATH, mtime)
    else:
        custom_table_view(load_samples(DB_PATH, mtime), load_file_paths(DB_PATH, mtime))


if __name__ == "__main__":
    main()

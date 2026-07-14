"""Read-only Streamlit GUI for browsing the Ocean DNA catalog.

Launch via `python odna.py gui --db PATH` (which sets ODNA_DB and prints the SSH
tunnel command). No SQL knowledge required: pick a view, search, filter, download CSV.

Views:
  Samples      one row per sample (CSV export carries full R1/R2 paths + owner)
  Projects     one row per sequencing project, with summary stats + owner
  Files        one row per FASTQ, full absolute path, size, owner, backup status
  Taxonomy     interactive breadth of NCBI-resolved sample taxonomy
  Custom table search + collect samples; export CSV + an rclone copy job for them
"""

import os
import sqlite3
import sys

import pandas as pd
import streamlit as st

# Reach the odna package (this app lives in data_management_db/app/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from odna import rclone as orclone  # noqa: E402

NCBI_TAX_URL = "https://www.ncbi.nlm.nih.gov/datasets/taxonomy/"
RANK_COLS = ["tax_domain", "tax_kingdom", "tax_phylum", "tax_class",
             "tax_order", "tax_family", "tax_genus", "tax_species"]
RANK_LABELS = ["domain", "kingdom", "phylum", "class",
               "order", "family", "genus", "species"]

DB_PATH = os.environ.get("ODNA_DB", "oceandna_catalog.db")

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
    "missing_mapfile": "no mapfile",
    "missing_seqdata": "no data folder",
    "broken_mapfile":  "broken mapfile",
}
_MAPFILE_DETAIL = {
    "missing_mapfile": "A project folder is on disk but has no '<project>_mapfile.csv' "
                       "in the metadata directory. Files were cataloged from disk; "
                       "sample metadata (taxon, UniqID) is missing until a mapfile is added.",
    "missing_seqdata": "A mapfile exists but no matching project folder was found in the "
                       "sequence-data directory. Samples were cataloged from the mapfile, "
                       "but no files are on disk.",
    "broken_mapfile":  "The mapfile is present but its header is malformed (expected "
                       "ID,R1,R2,Taxon,UniqID). Files were cataloged from disk; sample "
                       "metadata was skipped until the mapfile is fixed.",
}


def _sql(db_path, query):
    con = sqlite3.connect(db_path)
    try:
        return pd.read_sql_query(query, con)
    finally:
        con.close()


@st.cache_data(ttl=60)
def load_samples(db_path, mtime):
    return _sql(db_path, f"""
        SELECT s.project_id, s.sample_id, s.taxon, s.uniq_id,
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
        ORDER BY s.project_id, s.sample_id""")


@st.cache_data(ttl=60)
def load_taxonomy(db_path, mtime):
    return _sql(db_path, f"""
        SELECT s.project_id, s.sample_id, s.taxon, t.taxid, t.match_type,
               {', '.join('t.' + c for c in RANK_COLS)}
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
               (SELECT MAX(f.integrity_date) FROM files f
                  WHERE f.project_id = p.project_id) AS integrity_date,
               p.owner_name, p.seq_data_relpath AS data_dir,
               COALESCE(p.metadata_status, 'ok') AS metadata_status,
               p.metadata_detail,
               p.data_check_date, p.date_ingested
        FROM projects p
        ORDER BY p.project_id""")


@st.cache_data(ttl=60)
def load_data_issues(db_path, mtime):
    return _sql(db_path, f"""
        SELECT i.project_id, i.kind, i.filename,
               {_FULL_PATH} AS full_path,
               s.sample_id, f.direction, s.taxon, s.uniq_id
        FROM data_check_issues i
        LEFT JOIN files f ON f.project_id = i.project_id AND f.filename = i.filename
        LEFT JOIN projects p ON p.project_id = i.project_id
        LEFT JOIN samples s ON s.sample_pk = f.sample_pk
        ORDER BY i.project_id, i.kind, i.filename""")


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
               CASE f.md5_match WHEN 1 THEN 'OK' WHEN 0 THEN 'MISMATCH'
                    ELSE 'uncompared' END AS backup,
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
        mask = (view["sample_id"].str.lower().str.contains(s, na=False)
                | view["taxon"].str.lower().str.contains(s, na=False)
                | view["uniq_id"].str.lower().str.contains(s, na=False))
        view = view[mask]

    st.caption(f"{len(view)} of {len(df)} samples (full R1/R2 paths, taxid + "
               "lineage are in the CSV export). Select a row to list its files below.")
    on_screen = ["project_id", "sample_id", "taxon", "ncbi_url", "tax_match",
                 "uniq_id", "backup_verified"]
    event = st.dataframe(
        view[on_screen], width="stretch", hide_index=True,
        on_select="rerun", selection_mode="single-row", key="samples_table",
        column_config={
            "tax_match": "match type",
            # The matched name (carried in the URL fragment) is the link label,
            # linking to its NCBI datasets taxonomy page.
            "ncbi_url": st.column_config.LinkColumn(
                "NCBI taxon match", display_text=r"#(.+)$")})
    _download(view, "oceandna_samples.csv")

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
        mask = (view["project_id"].str.lower().str.contains(s, na=False)
                | view["description"].str.lower().str.contains(s, na=False))
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
    _download(show, "oceandna_projects.csv")

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
                     "direction", "taxon", "uniq_id"]],
                width="stretch", hide_index=True,
                column_config={"full_path": "expected path"})
            _download(sub, f"{pid}_data_issues.csv")
        else:
            st.info("No recorded data-files issues. "
                    "Run `validate --seqdata-root` to refresh.")


def files_view(df, samples_df):
    with st.sidebar:
        st.header("Filters")
        projects = sorted(df["project_id"].unique())
        chosen = st.multiselect("Project", projects)
        backup = st.selectbox("Backup status", ["All", "OK", "MISMATCH", "uncompared"])
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
        mask = (view["sample_id"].str.lower().str.contains(s, na=False)
                | view["filename"].str.lower().str.contains(s, na=False))
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
    _download(view, "oceandna_files.csv")

    sel = event.selection.rows if event and event.selection else []
    if not sel:
        return
    r = show.iloc[sel[0]]
    pid, sid = r["project_id"], r["sample_id"]
    ssub = samples_df[(samples_df["project_id"] == pid)
                      & (samples_df["sample_id"] == sid)]
    st.subheader(f"Sample info: {sid} ({pid})")
    if len(ssub):
        cols = [c for c in ["sample_id", "taxon", "ncbi_url", "tax_match",
                            "taxid", "lineage", "uniq_id", "backup_verified",
                            "r1_path", "r2_path"] if c in ssub.columns]
        st.dataframe(
            ssub[cols], width="stretch", hide_index=True,
            column_config={
                "tax_match": "match type",
                "ncbi_url": st.column_config.LinkColumn(
                    "NCBI taxon match", display_text=r"#(.+)$")})
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


def taxonomy_view(db_path, mtime):
    import plotly.express as px

    df = load_taxonomy(db_path, mtime)  # cached; used only for the project list
    with st.sidebar:
        st.header("Filters")
        projects = sorted(df["project_id"].unique())
        chosen = st.multiselect("Project", projects)
        depth_label = st.selectbox("Deepest rank", RANK_LABELS,
                                   index=RANK_LABELS.index("order"))
        include_unknown = st.checkbox("Include 'unknown'", value=True)
        ring_cap = st.slider(
            "Initial rings shown", 2, len(RANK_COLS), 4,
            help="The sunburst renders this many rings; click a wedge to drill deeper.")

    depth = RANK_LABELS.index(depth_label) + 1
    cols = RANK_COLS[:depth]
    counts, n_samples, n_proj = _taxonomy_counts(
        db_path, mtime, tuple(chosen), depth, include_unknown)

    st.caption(f"{n_samples} samples across {n_proj} project(s); "
               "run `taxonomy resolve` to (re)populate. Unranked -> 'unknown'.")
    if not n_samples:
        st.info("No resolved taxonomy yet. Run `odna.py taxonomy resolve`.")
        return

    fig = px.sunburst(counts, path=cols, values="samples")
    # Render only the outer `ring_cap` rings up front; deeper rings load on click.
    # At species depth the tree has thousands of leaf wedges, and drawing them all
    # is what makes the initial paint slow -- maxdepth caps the drawn arcs.
    fig.update_traces(maxdepth=min(ring_cap, depth))
    fig.update_layout(margin=dict(t=10, l=10, r=10, b=10))
    event = st.plotly_chart(fig, width="stretch", on_select="rerun", key="sunburst")

    # Clicking a wedge filters the bar chart below to that node's subtree, so it
    # tracks wherever the user drills in the sunburst. px.sunburst builds each
    # wedge id as the "/"-joined lineage from the root, which pins the exact
    # subtree; fall back to matching the label if no id comes back.
    lineage = _selected_lineage(event, counts, cols)
    sub = counts
    for col, val in zip(cols, lineage):
        sub = sub[sub[col] == val]

    if lineage:
        st.caption("Bar chart scoped to: " + " › ".join(lineage)
                   + "  — click the sunburst center to reset.")
    st.subheader(f"Sample count by {depth_label}")
    if sub.empty:
        st.info(f"No {depth_label}-level samples under this selection.")
    else:
        bar = (sub.groupby(cols[-1])["samples"].sum().rename_axis(depth_label)
               .reset_index(name="samples").set_index(depth_label))
        st.bar_chart(bar, width="stretch")
    _download(counts, "oceandna_taxonomy_counts.csv")


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
               f"Custom table currently holds {len(cart)} sample(s).")
    cols = ["project_id", "sample_id", "taxon", "tax_name", "uniq_id"]
    event = st.dataframe(
        view[cols], width="stretch", hide_index=True,
        on_select="rerun", selection_mode="multi-row", key="cart_search",
        column_config={"tax_name": "NCBI name"})

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
    st.subheader(f"Custom table ({len(cart)} sample(s))")
    if not cart:
        st.info("No samples yet. Search above and add some.")
        return

    keyset = {(v["project_id"], v["sample_id"]) for v in cart.values()}
    idx = samples_df.set_index(["project_id", "sample_id"]).index
    table = samples_df[idx.isin(keyset)].copy()

    tcols = ["project_id", "sample_id", "taxon", "tax_name", "taxid", "uniq_id",
             "lineage", "r1_path", "r2_path"]
    tcols = [c for c in tcols if c in table.columns]
    tevent = st.dataframe(
        table[tcols], width="stretch", hide_index=True,
        on_select="rerun", selection_mode="multi-row", key="cart_table")
    tsel = tevent.selection.rows if tevent and tevent.selection else []
    if st.button(f"➖ Remove selected ({len(tsel)})", disabled=not tsel):
        for i in tsel:
            r = table.iloc[i]
            cart.pop(_cart_key(r["project_id"], r["sample_id"]), None)
        st.rerun()
    st.download_button("Download custom table CSV",
                       table[tcols].to_csv(index=False).encode(),
                       "oceandna_custom_table.csv", "text/csv")

    # Files backing the selected samples: paths, sizes, source roots for rclone.
    fpaths = paths_df[paths_df.set_index(["project_id", "sample_id"]).index.isin(keyset)]
    rows = fpaths.to_dict("records")
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
    transfers = a1.slider("Parallel transfers", 1, 8, 4,
                          help="rclone --transfers; also the mthread slots requested.")
    mem = a2.slider("Memory per slot (GB)", 1, 8, 4, help="lTIO caps 8 GB/slot.")

    groups, n_unres = orclone.group_by_root(rows)
    if n_unres:
        st.caption(f"{n_unres} file(s) can't be copied (no known source root/path) "
                   "and are omitted from the script.")
    script = orclone.build_copy_script(
        groups, dest, transfers=transfers, slots=transfers, mem=mem,
        est_bytes=total, n_files=n_files)
    st.caption("Copy this into a `.job` file on Hydra and submit with "
               "`qsub <file>.job` from the login node (lTIO: 6 slots/user, 2 concurrent).")
    st.code(script, language="bash")
    st.download_button("Download submission script", script.encode(),
                       "odna_rclone_copy.job", "text/x-shellscript")


def main():
    st.set_page_config(page_title="Ocean DNA catalog", layout="wide")
    st.title("Ocean DNA raw sequence catalog")

    if not os.path.exists(DB_PATH):
        st.error(f"Catalog database not found: {DB_PATH}")
        return

    view_name = st.sidebar.radio(
        "View", ["Samples", "Projects", "Files", "Taxonomy", "Custom table"])
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

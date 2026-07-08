"""Read-only Streamlit GUI for browsing the Ocean DNA catalog.

Launch via `python odna.py gui --db PATH` (which sets ODNA_DB and prints the SSH
tunnel command). No SQL knowledge required: pick a view, search, filter, download CSV.

Views:
  Samples   one row per sample (CSV export carries full R1/R2 paths + owner)
  Projects  one row per sequencing project, with summary stats + owner
  Files     one row per FASTQ, full absolute path, size, owner, backup status
  Taxonomy  interactive breadth of NCBI-resolved sample taxonomy
"""

import os
import sqlite3

import pandas as pd
import streamlit as st

NCBI_TAX_URL = "https://www.ncbi.nlm.nih.gov/datasets/taxonomy/"
RANK_COLS = ["tax_domain", "tax_kingdom", "tax_phylum", "tax_class",
             "tax_order", "tax_family", "tax_genus", "tax_species"]
RANK_LABELS = ["domain", "kingdom", "phylum", "class",
               "order", "family", "genus", "species"]

DB_PATH = os.environ.get("ODNA_DB", "oceandna_catalog.db")

# Full absolute path when the seqdata_root was captured at ingest, else the relpath.
_FULL_PATH = "COALESCE(p.seqdata_root || '/' || f.rel_path, f.rel_path)"


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
                    THEN '{NCBI_TAX_URL}' || t.taxid || '/' END AS ncbi_url,
               (SELECT {_FULL_PATH} FROM files f
                  WHERE f.project_id = s.project_id AND f.sample_pk = s.sample_pk
                    AND f.role = 'R1') AS r1_path,
               (SELECT f.owner_name FROM files f
                  WHERE f.project_id = s.project_id AND f.sample_pk = s.sample_pk
                    AND f.role = 'R1') AS r1_owner,
               (SELECT {_FULL_PATH} FROM files f
                  WHERE f.project_id = s.project_id AND f.sample_pk = s.sample_pk
                    AND f.role = 'R2') AS r2_path,
               (SELECT f.owner_name FROM files f
                  WHERE f.project_id = s.project_id AND f.sample_pk = s.sample_pk
                    AND f.role = 'R2') AS r2_owner
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
               p.owner_name, p.seq_data_relpath AS data_dir,
               p.data_check_date, p.date_ingested
        FROM projects p
        ORDER BY p.project_id""")


@st.cache_data(ttl=60)
def load_data_issues(db_path, mtime):
    return _sql(db_path, """
        SELECT i.project_id, i.kind, i.filename,
               s.sample_id, f.role, s.taxon, s.uniq_id
        FROM data_check_issues i
        LEFT JOIN files f ON f.project_id = i.project_id AND f.filename = i.filename
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
        parts.append(f"{int(row['data_check_n_missing'])} missing")
    if row["data_check_n_orphan"]:
        parts.append(f"{int(row['data_check_n_orphan'])} orphan")
    return ", ".join(parts) or "issues"


def checksum_label(row):
    if row["n_files"] == 0:
        return "no files"
    if row["n_mismatch"]:
        return f"{int(row['n_mismatch'])} mismatch"
    if row["n_uncompared"]:
        return f"incomplete ({int(row['n_uncompared'])} uncompared)"
    return "verified"


@st.cache_data(ttl=60)
def load_files(db_path, mtime):
    return _sql(db_path, f"""
        SELECT f.project_id, s.sample_id, f.role, f.filename,
               {_FULL_PATH} AS full_path,
               f.size_bytes, f.owner_name,
               CASE f.md5_match WHEN 1 THEN 'OK' WHEN 0 THEN 'MISMATCH'
                    ELSE 'uncompared' END AS backup
        FROM files f
        JOIN projects p ON p.project_id = f.project_id
        LEFT JOIN samples s ON s.sample_pk = f.sample_pk
        ORDER BY f.project_id, s.sample_id, f.role""")


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


def samples_view(df):
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

    st.caption(f"{len(view)} of {len(df)} samples "
               "(full R1/R2 paths, taxid + lineage are in the CSV export)")
    on_screen = ["project_id", "sample_id", "taxon", "tax_name", "tax_match",
                 "uniq_id", "backup_verified", "ncbi_url"]
    st.dataframe(
        view[on_screen], width="stretch", hide_index=True,
        column_config={
            "tax_name": "NCBI match",
            "tax_match": "match type",
            "ncbi_url": st.column_config.LinkColumn("NCBI taxon", display_text="view")})
    _download(view, "oceandna_samples.csv")


def projects_view(df, issues):
    with st.sidebar:
        st.header("Filters")
        search = st.text_input("Search (project, description)")
        data_only = st.checkbox("Only data-files issues")
        cs_only = st.checkbox("Only checksum issues")
    view = df
    if search:
        s = search.lower()
        mask = (view["project_id"].str.lower().str.contains(s, na=False)
                | view["description"].str.lower().str.contains(s, na=False))
        view = view[mask]
    if data_only:
        view = view[view["data_check_status"] == "issues"]
    if cs_only:
        view = view[(view["n_mismatch"] > 0) | (view["n_uncompared"] > 0)]

    show = view.copy()
    show["data_files"] = show.apply(data_files_label, axis=1) if len(show) else []
    show["checksum"] = show.apply(checksum_label, axis=1) if len(show) else []
    st.caption(f"{len(view)} of {len(df)} projects. Select a row to list its "
               "data_files issues below (missing / orphan files).")
    cols = ["project_id", "source", "description", "n_samples", "n_files",
            "data_files", "checksum", "owner_name", "data_dir", "date_ingested"]
    event = st.dataframe(show[cols], width="stretch", hide_index=True,
                         on_select="rerun", selection_mode="single-row",
                         key="projects_table")
    _download(show, "oceandna_projects.csv")

    sel = event.selection.rows if event and event.selection else []
    if sel:
        pid = show.iloc[sel[0]]["project_id"]
        sub = issues[issues["project_id"] == pid]
        st.subheader(f"Data-files issues: {pid}")
        if len(sub):
            st.dataframe(
                sub[["kind", "filename", "sample_id", "role", "taxon", "uniq_id"]],
                width="stretch", hide_index=True)
            _download(sub, f"{pid}_data_issues.csv")
        else:
            st.info("No recorded data-files issues. "
                    "Run `validate --seqdata-root` to refresh.")


def files_view(df):
    with st.sidebar:
        st.header("Filters")
        projects = sorted(df["project_id"].unique())
        chosen = st.multiselect("Project", projects)
        backup = st.selectbox("Backup status", ["All", "OK", "MISMATCH", "uncompared"])
        search = st.text_input("Search (sample, filename)")
    view = df
    if chosen:
        view = view[view["project_id"].isin(chosen)]
    if backup != "All":
        view = view[view["backup"] == backup]
    if search:
        s = search.lower()
        mask = (view["sample_id"].str.lower().str.contains(s, na=False)
                | view["filename"].str.lower().str.contains(s, na=False))
        view = view[mask]
    st.caption(f"{len(view)} of {len(df)} files")
    show = view.copy()
    show["size"] = show["size_bytes"].map(human_size)
    st.dataframe(
        show[["project_id", "sample_id", "role", "filename", "full_path",
              "size", "owner_name", "backup"]],
        width="stretch", hide_index=True)
    _download(view, "oceandna_files.csv")


def taxonomy_view(df):
    import plotly.express as px

    with st.sidebar:
        st.header("Filters")
        projects = sorted(df["project_id"].unique())
        chosen = st.multiselect("Project", projects)
        depth_label = st.selectbox("Deepest rank", RANK_LABELS,
                                   index=RANK_LABELS.index("order"))
        include_unknown = st.checkbox("Include 'unknown'", value=True)

    view = df
    if chosen:
        view = view[view["project_id"].isin(chosen)]

    depth = RANK_LABELS.index(depth_label) + 1
    cols = RANK_COLS[:depth]
    v = view.copy()
    for c in cols:
        v[c] = v[c].replace("", pd.NA).fillna("unknown")
    if not include_unknown:
        v = v[v[cols[-1]] != "unknown"]

    st.caption(f"{len(v)} samples across {v['project_id'].nunique()} project(s); "
               "run `taxonomy resolve` to (re)populate. Unranked -> 'unknown'.")
    if not len(v):
        st.info("No resolved taxonomy yet. Run `odna.py taxonomy resolve`.")
        return

    counts = v.groupby(cols).size().reset_index(name="samples")
    fig = px.sunburst(counts, path=cols, values="samples")
    fig.update_layout(margin=dict(t=10, l=10, r=10, b=10))
    st.plotly_chart(fig, width="stretch")

    st.subheader(f"Sample count by {depth_label}")
    bar = (v[cols[-1]].value_counts().rename_axis(depth_label)
           .reset_index(name="samples").set_index(depth_label))
    st.bar_chart(bar, width="stretch")
    _download(counts, "oceandna_taxonomy_counts.csv")


def main():
    st.set_page_config(page_title="Ocean DNA catalog", layout="wide")
    st.title("Ocean DNA raw sequence catalog")

    if not os.path.exists(DB_PATH):
        st.error(f"Catalog database not found: {DB_PATH}")
        return

    view_name = st.sidebar.radio("View", ["Samples", "Projects", "Files", "Taxonomy"])
    mtime = os.path.getmtime(DB_PATH)
    if view_name == "Samples":
        samples_view(load_samples(DB_PATH, mtime))
    elif view_name == "Projects":
        projects_view(load_projects(DB_PATH, mtime),
                      load_data_issues(DB_PATH, mtime))
    elif view_name == "Files":
        files_view(load_files(DB_PATH, mtime))
    else:
        taxonomy_view(load_taxonomy(DB_PATH, mtime))


if __name__ == "__main__":
    main()

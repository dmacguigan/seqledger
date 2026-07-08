"""Read-only Streamlit GUI for browsing the Ocean DNA catalog.

Launch via `python odna.py gui --db PATH` (which sets ODNA_DB and prints the SSH
tunnel command). No SQL knowledge required: pick a view, search, filter, download CSV.

Views:
  Samples   one row per sample (CSV export carries full R1/R2 paths + owner)
  Projects  one row per sequencing project, with summary stats + owner
  Files     one row per FASTQ, full absolute path, size, owner, backup status
"""

import os
import sqlite3

import pandas as pd
import streamlit as st

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
        ORDER BY s.project_id, s.sample_id""")


@st.cache_data(ttl=60)
def load_projects(db_path, mtime):
    return _sql(db_path, """
        SELECT p.project_id, p.source, p.description,
               (SELECT COUNT(*) FROM samples s WHERE s.project_id = p.project_id) AS n_samples,
               (SELECT COUNT(*) FROM files f WHERE f.project_id = p.project_id) AS n_files,
               COALESCE(b.verified, 0) AS backup_verified,
               COALESCE(b.n_mismatch, 0) AS n_mismatch,
               p.owner_name, p.seq_data_relpath AS data_dir, p.date_ingested
        FROM projects p
        LEFT JOIN backups b ON b.project_id = p.project_id AND b.location = 'pdrive'
        ORDER BY p.project_id""")


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
               "(full R1/R2 paths + owner are in the CSV export)")
    on_screen = ["project_id", "sample_id", "taxon", "uniq_id",
                 "source", "data_dir", "backup_verified"]
    st.dataframe(view[on_screen], width="stretch", hide_index=True)
    _download(view, "oceandna_samples.csv")


def projects_view(df):
    with st.sidebar:
        st.header("Filters")
        search = st.text_input("Search (project, description)")
    view = df
    if search:
        s = search.lower()
        mask = (view["project_id"].str.lower().str.contains(s, na=False)
                | view["description"].str.lower().str.contains(s, na=False))
        view = view[mask]
    st.caption(f"{len(view)} of {len(df)} projects")
    st.dataframe(view, width="stretch", hide_index=True)
    _download(view, "oceandna_projects.csv")


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


def main():
    st.set_page_config(page_title="Ocean DNA catalog", layout="wide")
    st.title("Ocean DNA raw sequence catalog")

    if not os.path.exists(DB_PATH):
        st.error(f"Catalog database not found: {DB_PATH}")
        return

    view_name = st.sidebar.radio("View", ["Samples", "Projects", "Files"])
    mtime = os.path.getmtime(DB_PATH)
    if view_name == "Samples":
        samples_view(load_samples(DB_PATH, mtime))
    elif view_name == "Projects":
        projects_view(load_projects(DB_PATH, mtime))
    else:
        files_view(load_files(DB_PATH, mtime))


if __name__ == "__main__":
    main()

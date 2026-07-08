"""Read-only Streamlit GUI for browsing the Ocean DNA catalog.

Launch via `python odna.py gui --db PATH` (which sets ODNA_DB and prints the SSH
tunnel command). No SQL knowledge required: search, filter, and download CSV.
"""

import os
import sqlite3

import pandas as pd
import streamlit as st

DB_PATH = os.environ.get("ODNA_DB", "oceandna_catalog.db")


@st.cache_data(ttl=60)
def load_samples(db_path, mtime):
    con = sqlite3.connect(db_path)
    try:
        return pd.read_sql_query(
            """SELECT s.project_id, s.sample_id, s.taxon, s.uniq_id,
                      p.source, COALESCE(b.verified, 0) AS backup_verified
               FROM samples s
               JOIN projects p ON p.project_id = s.project_id
               LEFT JOIN backups b ON b.project_id = s.project_id AND b.location = 'pdrive'
               ORDER BY s.project_id, s.sample_id""",
            con)
    finally:
        con.close()


def main():
    st.set_page_config(page_title="Ocean DNA catalog", layout="wide")
    st.title("Ocean DNA raw sequence catalog")

    if not os.path.exists(DB_PATH):
        st.error(f"Catalog database not found: {DB_PATH}")
        return

    df = load_samples(DB_PATH, os.path.getmtime(DB_PATH))

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

    st.caption(f"{len(view)} of {len(df)} samples")
    st.dataframe(view, use_container_width=True, hide_index=True)
    st.download_button("Download CSV", view.to_csv(index=False).encode(),
                       "oceandna_samples.csv", "text/csv")


if __name__ == "__main__":
    main()

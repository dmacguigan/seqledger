import os

from odna import db as odb


def _cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_adds_columns_to_old_db(tmp_path):
    conn = odb.connect(os.path.join(tmp_path, "old.db"))
    # Simulate a pre-migration catalog: projects/files without owner columns.
    conn.executescript("""
        CREATE TABLE projects (project_id TEXT PRIMARY KEY, source TEXT,
            seq_data_relpath TEXT);
        CREATE TABLE files (file_pk INTEGER PRIMARY KEY, project_id TEXT,
            sample_pk INTEGER, filename TEXT, rel_path TEXT, size_bytes INTEGER);
    """)
    conn.commit()
    assert "owner_name" not in _cols(conn, "files")

    odb.init_db(conn)  # runs migrations
    assert {"seqdata_root", "owner_uid", "owner_name"} <= _cols(conn, "projects")
    assert {"owner_uid", "owner_name"} <= _cols(conn, "files")

    # idempotent: second run does not error or duplicate
    odb.init_db(conn)
    assert {"owner_uid", "owner_name"} <= _cols(conn, "files")

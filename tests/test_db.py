import os
import sqlite3

import pytest

from seqledger import db as odb


def _cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _fresh_db(tmp_path):
    conn = odb.connect(os.path.join(tmp_path, "cat.db"))
    odb.init_db(conn)
    return conn


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


# --- #15: CHECK constraints on freshly-created catalogs ---

def test_check_rejects_bad_files_flags(tmp_path):
    conn = _fresh_db(tmp_path)
    conn.execute("INSERT INTO projects(project_id) VALUES ('p1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO files(project_id, filename, md5_match) VALUES ('p1','a.fastq.gz',5)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO files(project_id, filename, gz_ok) VALUES ('p1','b.fastq.gz',7)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO files(project_id, filename, direction) VALUES ('p1','c.fastq.gz','R3')")


def test_check_allows_valid_and_null_files_flags(tmp_path):
    conn = _fresh_db(tmp_path)
    conn.execute("INSERT INTO projects(project_id) VALUES ('p1')")
    # R1/R2/NULL directions and 0/1/NULL flags are all accepted (rel_path is NOT NULL)
    conn.execute("INSERT INTO files(project_id, filename, rel_path, direction, md5_match, gz_ok) "
                 "VALUES ('p1','r1.fastq.gz','p1/r1.fastq.gz','R1',1,1)")
    conn.execute("INSERT INTO files(project_id, filename, rel_path, direction, md5_match, gz_ok) "
                 "VALUES ('p1','r2.fastq.gz','p1/r2.fastq.gz','R2',0,0)")
    conn.execute("INSERT INTO files(project_id, filename, rel_path, direction, md5_match, gz_ok) "
                 "VALUES ('p1','x.fastq.gz','p1/x.fastq.gz',NULL,NULL,NULL)")
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 3


def test_check_rejects_bad_backups_and_taxa(tmp_path):
    conn = _fresh_db(tmp_path)
    conn.execute("INSERT INTO projects(project_id) VALUES ('p1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO backups(project_id, location, verified) "
                     "VALUES ('p1','pdrive',5)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO taxa(taxon, confirmed) VALUES ('Gadus morhua',2)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO taxa(taxon, worms_confirmed) VALUES ('Gadus ogac',3)")


# --- #34: case-insensitive / whitespace-tolerant mapfile header matching ---

def test_header_uniqid_column_case_insensitive():
    # all-lowercase header still matches; returns the fifth cell verbatim
    assert odb.header_uniqid_column(["id", "r1", "r2", "taxon", "uniqid"]) == "uniqid"
    # canonical case still works
    assert odb.header_uniqid_column(["ID", "R1", "R2", "Taxon", "UniqID"]) == "UniqID"
    # mixed case + surrounding whitespace tolerated; UniqueID alias accepted
    assert odb.header_uniqid_column([" Id ", "R1", "r2", "TAXON", " UniqueID"]) == " UniqueID"


def test_header_uniqid_column_rejects_bad_header():
    assert odb.header_uniqid_column(["ID", "R1", "R2", "Taxon"]) is None  # too short
    assert odb.header_uniqid_column(["ID", "R1", "R2", "Species", "UniqID"]) is None
    assert odb.header_uniqid_column(["ID", "R1", "R2", "Taxon", "Voucher"]) is None


# --- #18: connect_ro refuses writes ---

def test_connect_ro_refuses_writes(tmp_path):
    path = os.path.join(tmp_path, "cat.db")
    conn = odb.connect(path)
    odb.init_db(conn)
    conn.execute("INSERT INTO projects(project_id) VALUES ('p1')")
    conn.commit()
    conn.close()

    ro = odb.connect_ro(path)
    # reads still work
    assert ro.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
    # writes are refused (query_only=ON on top of mode=ro)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO projects(project_id) VALUES ('p2')")


_OLD_FILES_DDL = """
DROP TABLE files;
CREATE TABLE files (
  file_pk INTEGER PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  sample_pk INTEGER REFERENCES samples(sample_pk) ON DELETE CASCADE,
  direction TEXT, filename TEXT NOT NULL, rel_path TEXT, size_bytes INTEGER,
  owner_uid INTEGER, owner_name TEXT, md5 TEXT, md5_source TEXT, store_md5 TEXT,
  pdrive_md5 TEXT, md5_match INTEGER, date_hashed TEXT, integrity_status TEXT,
  gz_ok INTEGER, n_reads INTEGER, integrity_date TEXT,
  UNIQUE (project_id, filename));
"""


def test_migrate_files_relpath_unique_rebuild(tmp_path):
    import sqlite3
    import pytest
    conn = _fresh_db(tmp_path)          # current schema (rel_path UNIQUE)
    conn.executescript(_OLD_FILES_DDL)  # downgrade files to the legacy basename key
    conn.execute("INSERT INTO projects(project_id) VALUES ('p')")
    conn.execute("INSERT INTO samples(sample_pk, project_id, sample_id) VALUES (5,'p','s1')")
    conn.execute("INSERT INTO files(file_pk, project_id, sample_pk, filename, rel_path, md5_match) "
                 "VALUES (50,'p',5,'x.fastq.gz','p/sub/x.fastq.gz',1)")
    conn.execute("INSERT INTO files(file_pk, project_id, sample_pk, filename, rel_path) "
                 "VALUES (51,'p',5,'y.fastq.gz',NULL)")  # legacy NULL rel_path
    conn.commit()
    assert ["project_id", "filename"] in odb._files_unique_keys(conn)

    odb.init_db(conn)  # triggers the table rebuild

    keys = odb._files_unique_keys(conn)
    assert ["project_id", "rel_path"] in keys
    assert ["project_id", "filename"] not in keys
    got = {r["file_pk"]: (r["filename"], r["rel_path"], r["md5_match"])
           for r in conn.execute("SELECT file_pk, filename, rel_path, md5_match FROM files")}
    assert got[50] == ("x.fastq.gz", "p/sub/x.fastq.gz", 1)   # data + file_pk preserved
    assert got[51][1] == "y.fastq.gz"                          # NULL rel_path backfilled
    # same-basename / different rel_path now coexist; duplicate rel_path rejected
    conn.execute("INSERT INTO files(project_id, sample_pk, filename, rel_path) "
                 "VALUES ('p',5,'x.fastq.gz','p/other/x.fastq.gz')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO files(project_id, filename, rel_path) "
                     "VALUES ('p','z.fastq.gz','p/sub/x.fastq.gz')")
    # idempotent: a second init-db is a no-op
    odb.init_db(conn)
    assert ["project_id", "rel_path"] in odb._files_unique_keys(conn)


def test_migrate_files_relpath_refuses_on_duplicate(tmp_path):
    import pytest
    conn = _fresh_db(tmp_path)
    conn.executescript(_OLD_FILES_DDL)
    conn.execute("INSERT INTO projects(project_id) VALUES ('p')")
    conn.execute("INSERT INTO files(project_id, filename, rel_path) VALUES ('p','a.gz','p/x.gz')")
    conn.execute("INSERT INTO files(project_id, filename, rel_path) VALUES ('p','b.gz','p/x.gz')")
    conn.commit()
    with pytest.raises(RuntimeError):  # refuses rather than a mid-rebuild IntegrityError
        odb.init_db(conn)

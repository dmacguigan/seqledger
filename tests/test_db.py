import os
import sqlite3

import pytest

from seqledger import db as odb


def test_init_db_refuses_old_catalog(tmp_path):
    conn = odb.connect(os.path.join(tmp_path, "old.db"))
    conn.execute("CREATE TABLE files (file_pk INTEGER PRIMARY KEY, project_id TEXT, filename TEXT)")
    with pytest.raises(RuntimeError, match="older seqledger"):
        odb.init_db(conn)


# --- #15: CHECK constraints on freshly-created catalogs ---

def test_check_rejects_bad_files_flags(fresh_db):
    conn = fresh_db
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


def test_check_allows_valid_and_null_files_flags(fresh_db):
    conn = fresh_db
    conn.execute("INSERT INTO projects(project_id) VALUES ('p1')")
    # R1/R2/NULL directions and 0/1/NULL flags are all accepted (rel_path is NOT NULL)
    conn.execute("INSERT INTO files(project_id, filename, rel_path, direction, md5_match, gz_ok) "
                 "VALUES ('p1','r1.fastq.gz','p1/r1.fastq.gz','R1',1,1)")
    conn.execute("INSERT INTO files(project_id, filename, rel_path, direction, md5_match, gz_ok) "
                 "VALUES ('p1','r2.fastq.gz','p1/r2.fastq.gz','R2',0,0)")
    conn.execute("INSERT INTO files(project_id, filename, rel_path, direction, md5_match, gz_ok) "
                 "VALUES ('p1','x.fastq.gz','p1/x.fastq.gz',NULL,NULL,NULL)")
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 3


def test_check_rejects_bad_backups_and_taxa(fresh_db):
    conn = fresh_db
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_init_db_new_catalog_is_0600(tmp_path):
    p = os.path.join(tmp_path, "new.db")
    odb.init_db(odb.connect(p))
    assert os.stat(p).st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_init_db_leaves_existing_catalog_perms(tmp_path):
    p = os.path.join(tmp_path, "old.db")
    odb.init_db(odb.connect(p))
    os.chmod(p, 0o644)
    odb.init_db(odb.connect(p))
    assert os.stat(p).st_mode & 0o777 == 0o644


def test_writer_busy_timeout_is_60s(tmp_path):
    conn = odb.connect(os.path.join(tmp_path, "w.db"))
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 60000


def test_ro_uri_escapes_question_and_hash(tmp_path):
    d = tmp_path / "we?ird#dir"
    d.mkdir()
    p = str(d / "cat.db?x=1")
    odb.init_db(odb.connect(p))
    raw = sqlite3.connect(odb.ro_uri(p), uri=True)
    assert raw.execute("SELECT count(*) FROM sqlite_master WHERE name='files'").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        raw.execute("CREATE TABLE evil(x)")
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(odb.ro_uri(str(d / "missing.db")), uri=True).execute("SELECT 1")
    assert not (d / "missing.db").exists()

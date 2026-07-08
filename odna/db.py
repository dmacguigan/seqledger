"""Database helpers and shared constants for the Ocean DNA catalog."""

import os
import re
import sqlite3

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "schema.sql")

# Map file required columns. The fifth column is accepted as either "UniqID"
# (as documented in the data management guide) or "UniqueID" (as the old
# validate_seq_data.py expected). Confirm against a live map file if unsure.
REQUIRED_COLUMNS = ["ID", "R1", "R2", "Taxon"]
UNIQID_ALIASES = ("UniqID", "UniqueID")

METADATA_SUFFIX = "_mapfile.csv"

# Regexes to parse a project_id into source / number / description.
_GENOHUB_RE = re.compile(r"^genohub-(\d+)_(.+)$")
_LAB_RE = re.compile(r"^LAB-([^_]+)_(.+)$")


def connect(db_path):
    """Open a SQLite connection with foreign keys on and Row factory."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the initial schema. Applied to pre-existing DBs on init.
_MIGRATIONS = [
    ("projects", "seqdata_root", "TEXT"),
    ("projects", "owner_uid", "INTEGER"),
    ("projects", "owner_name", "TEXT"),
    ("projects", "data_check_status", "TEXT"),
    ("projects", "data_check_n_missing", "INTEGER"),
    ("projects", "data_check_n_orphan", "INTEGER"),
    ("projects", "data_check_date", "TEXT"),
    ("files", "owner_uid", "INTEGER"),
    ("files", "owner_name", "TEXT"),
]


def _migrate(conn):
    """Add any columns missing from an older DB (idempotent)."""
    for table, column, decl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(conn, schema_path=SCHEMA_PATH):
    """Create tables from schema.sql, then apply migrations (idempotent)."""
    with open(schema_path) as f:
        conn.executescript(f.read())
    _migrate(conn)
    conn.commit()


def header_uniqid_column(header):
    """Return the name of the fifth (UniqID) column if the header is valid, else None."""
    if len(header) < 5:
        return None
    if [h.strip() for h in header[:4]] != REQUIRED_COLUMNS:
        return None
    fifth = header[4].strip()
    return fifth if fifth in UNIQID_ALIASES else None


def parse_project_id(seq_data_relpath):
    """Derive project_id (top-level dir) and source/number/description from a data dir path.

    seq_data_relpath may be nested, e.g. "genohub-2054899_OKEX01/22030-14-...".
    The project_id is always the top-level directory.
    """
    project_id = seq_data_relpath.strip("/").split("/")[0]
    source = number = description = None
    m = _GENOHUB_RE.match(project_id)
    if m:
        source, number, description = "genohub", m.group(1), m.group(2)
    else:
        m = _LAB_RE.match(project_id)
        if m:
            source, number, description = "LAB", m.group(1), m.group(2)
    return project_id, source, number, description

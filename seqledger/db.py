"""Database helpers and shared constants for the Ocean DNA sequence data catalog."""

import os
import re
import sqlite3

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

# Map file required columns. The fifth column is accepted as either "UniqID"
# (as documented in the data management guide) or "UniqueID" (as the old
# validate_seq_data.py expected). Confirm against a live map file if unsure.
REQUIRED_COLUMNS = ["ID", "R1", "R2", "Taxon"]
UNIQID_ALIASES = ("UniqID", "UniqueID")

METADATA_SUFFIX = "_mapfile.csv"

# Regexes to parse a project_id into source / number / description.
_GENOHUB_RE = re.compile(r"^genohub-(\d+)_(.+)$")
_LAB_RE = re.compile(r"^LAB-([^_]+)_(.+)$")

# Per-catalog configuration (key/value in the `config` table). Every default here
# is today's hardcoded value, so an existing catalog with no config rows behaves
# exactly as before. Set at `init-db`; read by the CLI, GUI, and script builders
# so a second lab can retarget the tool without editing source.
CONFIG_DEFAULTS = {
    "catalog_name":    "Ocean DNA sequence data catalog",  # GUI title / CLI banner
    "catalog_slug":    "oceandna",                         # export-filename prefix
    "seqdata_root":    "",                                 # default --seqdata-root
    "metadata_root":   "",                                 # default --metadata-root
    "conda_env":       "seqledger",                        # activated in qsub jobs
    "rclone_module":   "tools/rclone/1.66.0",              # module load in copy jobs
    "login_host":      "hydra-login01.si.edu",             # GUI SSH-tunnel host
    "io_queue":        "lTIO.sq",                          # qsub queue for batch/rclone/gui
    "backup_location": "pdrive",                            # 'verified backup' location label
    "fastq_extensions": "fastq.gz,fq.gz",                  # comma-sep FASTQ suffixes
}


def get_config(conn, key, default=None):
    """Config value for key, falling back to CONFIG_DEFAULTS then `default`.

    Tolerates a missing `config` table (an older read-only catalog copy that
    predates it), so the GUI on a stale Scratch replica still resolves defaults.
    """
    try:
        row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is not None and row["value"] is not None:
        return row["value"]
    return CONFIG_DEFAULTS.get(key, default)


def set_config(conn, key, value):
    """Upsert one config key (no commit; caller commits)."""
    conn.execute(
        "INSERT INTO config(key, value, updated_at) VALUES (?, ?, date('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value))


def resolve_config(conn):
    """All config keys as a dict (defaults merged with any stored rows)."""
    cfg = dict(CONFIG_DEFAULTS)
    try:
        for r in conn.execute("SELECT key, value FROM config"):
            if r["value"] is not None:
                cfg[r["key"]] = r["value"]
    except sqlite3.OperationalError:
        pass
    return cfg


def fastq_globs(exts_csv):
    """Turn a 'fastq.gz,fq.gz' config string into ['*.fastq.gz', '*.fq.gz']."""
    return ["*." + e.strip().lstrip(".") for e in (exts_csv or "").split(",") if e.strip()]


def connect(db_path):
    """Open a SQLite connection with foreign keys on and Row factory."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def connect_ro(db_path):
    """Open the catalog read-only (mode=ro): no writes, no migrate, no file creation.

    For paths that must not write the shared DB over NFS -- notably the remote
    integrity batch worker (--emit-json), which only reads its file list. mode=ro
    (not immutable) so it stays correct while the CLI writes the master elsewhere.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # Defense in depth on top of mode=ro: refuse any write attempt outright.
    # (foreign_keys is irrelevant on a pure-read connection, so it stays off.)
    conn.execute("PRAGMA query_only = ON")
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
    ("projects", "metadata_status", "TEXT"),
    ("projects", "metadata_detail", "TEXT"),
    ("samples", "flags", "TEXT"),
    ("files", "owner_uid", "INTEGER"),
    ("files", "owner_name", "TEXT"),
    ("files", "integrity_status", "TEXT"),
    ("files", "gz_ok", "INTEGER"),
    ("files", "n_reads", "INTEGER"),
    ("files", "integrity_date", "TEXT"),
    # WoRMS resolution columns on `taxa` (parallel to the NCBI columns), added by
    # `taxonomy resolve --source worms`. Existing catalogs pick these up on init-db.
    ("taxa", "aphia_id", "INTEGER"),
    ("taxa", "worms_sci_name", "TEXT"),
    ("taxa", "worms_status", "TEXT"),
    ("taxa", "worms_match_type", "TEXT"),
    ("taxa", "worms_rank", "TEXT"),
    ("taxa", "worms_kingdom", "TEXT"),
    ("taxa", "worms_phylum", "TEXT"),
    ("taxa", "worms_class", "TEXT"),
    ("taxa", "worms_order", "TEXT"),
    ("taxa", "worms_family", "TEXT"),
    ("taxa", "worms_genus", "TEXT"),
    ("taxa", "worms_species", "TEXT"),
    ("taxa", "worms_lineage", "TEXT"),
    ("taxa", "worms_alternatives", "TEXT"),
    ("taxa", "worms_is_marine", "INTEGER"),
    ("taxa", "worms_confirmed", "INTEGER DEFAULT 0"),
    ("taxa", "worms_date_resolved", "TEXT"),
]


def _migrate(conn):
    """Add any columns missing from an older DB (idempotent)."""
    # The table/column/decl identifiers interpolated into the f-string DDL below all
    # come from the hardcoded _MIGRATIONS constant (never user input), so building the
    # statements with f-strings is safe -- SQLite has no parameter binding for DDL.
    for table, column, decl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not cols:
            continue  # table doesn't exist in this DB -> nothing to migrate
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    # Settle the R1/R2 column name on 'direction' (was 'role', briefly 'read').
    fcols = {r["name"] for r in conn.execute("PRAGMA table_info(files)")}
    if "direction" not in fcols:
        if "role" in fcols:
            conn.execute("ALTER TABLE files RENAME COLUMN role TO direction")
        elif "read" in fcols:
            conn.execute("ALTER TABLE files RENAME COLUMN read TO direction")
        else:
            # No known legacy name to rename; add the column so downstream queries
            # (which all reference files.direction) never hit 'no such column'.
            conn.execute("ALTER TABLE files ADD COLUMN direction TEXT")

    # Partial index for `query mismatches`; created here (not in schema.sql) and
    # guarded, because it references md5_match, which a partial/older `files` table
    # may not have (schema.sql's executescript would fail on it).
    if "md5_match" in {r["name"] for r in conn.execute("PRAGMA table_info(files)")}:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_mismatch "
                     "ON files(md5_match) WHERE md5_match = 0")


def init_db(conn, schema_path=SCHEMA_PATH):
    """Create tables from schema.sql, then apply migrations (idempotent)."""
    with open(schema_path) as f:
        conn.executescript(f.read())
    _migrate(conn)
    conn.commit()


def header_uniqid_column(header):
    """Return the name of the fifth (UniqID) column if the header is valid, else None.

    Matching is case-insensitive and whitespace-tolerant so the tool retargets to a
    second lab's mapfiles (per the README) that differ only in case/spacing. The name
    returned is the header cell verbatim (not normalized): callers use it as a dict key
    against the parsed row, whose keys are the original header cells.
    """
    if len(header) < 5:
        return None
    required_lower = [c.lower() for c in REQUIRED_COLUMNS]
    if [h.strip().lower() for h in header[:4]] != required_lower:
        return None
    fifth = header[4]
    aliases_lower = {a.lower() for a in UNIQID_ALIASES}
    return fifth if fifth.strip().lower() in aliases_lower else None


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

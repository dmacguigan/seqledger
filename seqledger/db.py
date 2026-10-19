"""Database helpers and shared constants for the Ocean DNA sequence data catalog."""

import os
import re
import sqlite3
import urllib.parse

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
    """Config value for key, falling back to CONFIG_DEFAULTS then `default`."""
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    if row is not None and row["value"] is not None:
        return row["value"]
    return CONFIG_DEFAULTS.get(key, default)


# Config keys pasted into generated shell/qsub scripts; values must match SCRIPT_SAFE_RE.
SCRIPT_CONFIG_KEYS = ("conda_env", "io_queue", "rclone_module", "login_host", "catalog_slug")
SCRIPT_SAFE_RE = re.compile(r"[A-Za-z0-9._@:/-]+")


def check_config_value(key, value):
    """Raise ValueError if a config value is unsafe to store."""
    value = str(value)
    if any(c in value for c in "\n\r\0"):
        raise ValueError(f"config {key}: value must not contain a newline, carriage return, or NUL")
    if key in SCRIPT_CONFIG_KEYS and not SCRIPT_SAFE_RE.fullmatch(value):
        raise ValueError(f"config {key}={value!r}: only letters, digits, and . _ @ : / - are allowed")


def check_directive(value):
    """Return value for an SGE '#$' line; raise ValueError on whitespace/newlines."""
    value = str(value)
    if not value or re.search(r"\s|\0", value):
        raise ValueError(f"unsafe value for a qsub directive: {value!r}")
    return value


def qsub_header(name, log, queue, mem, slots=None):
    """Shebang plus the SGE '#$' block for an I/O-queue job (mem is GB per slot)."""
    name, log, queue = (check_directive(v) for v in (name, log, queue))
    pe = f"#$ -pe mthread {slots}\n" if slots else ""
    return (f"#!/bin/bash\n#$ -N {name}\n#$ -o {log}\n#$ -j y\n#$ -terse\n#$ -notify\n"
            f"{pe}#$ -q {queue} -l ioq\n"
            f"#$ -l mres={mem * (slots or 1)}G,h_data={mem}G,h_vmem={mem}G\n"
            "#$ -S /bin/bash\n#$ -cwd\n")


def set_config(conn, key, value):
    """Upsert one config key (no commit; caller commits)."""
    check_config_value(key, value)
    conn.execute(
        "INSERT INTO config(key, value, updated_at) VALUES (?, ?, date('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value))


def resolve_config(conn):
    """All config keys as a dict (defaults merged with any stored rows)."""
    cfg = dict(CONFIG_DEFAULTS)
    for r in conn.execute("SELECT key, value FROM config"):
        if r["value"] is not None:
            cfg[r["key"]] = r["value"]
    return cfg


def fastq_globs(exts_csv):
    """Turn a 'fastq.gz,fq.gz' config string into ['*.fastq.gz', '*.fq.gz'] (default if empty)."""
    return (["*." + e.strip().lstrip(".") for e in (exts_csv or "").split(",") if e.strip()]
            or fastq_globs(CONFIG_DEFAULTS["fastq_extensions"]))


def read_text(path):
    """Read a text file, tolerating UTF-16 (BOM), UTF-8 BOM, and cp1252 exports."""
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def connect(db_path):
    """Open a SQLite connection with foreign keys on and Row factory."""
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ro_uri(db_path):
    """SQLite read-only URI for db_path, with ?/# in the path escaped."""
    return f"file:{urllib.parse.quote(db_path, safe='/')}?mode=ro"


def connect_ro(db_path):
    """Open the catalog read-only (mode=ro): no writes, no migrate, no file creation.

    For paths that must not write the shared DB over NFS -- notably the remote
    integrity batch worker (--emit-json), which only reads its file list. mode=ro
    (not immutable) so it stays correct while the CLI writes the master elsewhere;
    timeout lets SQLite wait out a concurrent write lock.
    """
    conn = sqlite3.connect(ro_uri(db_path), uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    # Defense in depth on top of mode=ro: refuse any write attempt outright.
    # (foreign_keys is irrelevant on a pure-read connection, so it stays off.)
    conn.execute("PRAGMA query_only = ON")
    return conn


def init_db(conn, schema_path=SCHEMA_PATH):
    """Create tables from schema.sql (idempotent on a current catalog)."""
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    new = bool(path) and os.path.getsize(path) == 0
    with open(schema_path) as f:
        schema = f.read()
    ref = sqlite3.connect(":memory:")
    ref.executescript(schema)
    for (table,) in ref.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if have and {r[1] for r in ref.execute(f"PRAGMA table_info({table})")} - have:
            raise RuntimeError(f"catalog table {table!r} is from an older seqledger; "
                               "run init-db with seqledger 0.1.0 first")
    conn.executescript(schema)
    conn.commit()
    if new:
        os.chmod(path, 0o600)


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

-- Ocean DNA raw sequence data catalog
-- SQLite schema. Source of truth for the catalog structure.
--
-- NOTE: the CHECK constraints below apply to NEW catalogs only. init_db runs this
-- with CREATE TABLE IF NOT EXISTS, and SQLite cannot ALTER-ADD a CHECK to an
-- existing table, so pre-existing catalogs keep their (unchecked) tables unchanged.

PRAGMA foreign_keys = ON;

-- Per-catalog configuration (key/value). Written at init-db; unset keys fall back
-- to seqledger.db.CONFIG_DEFAULTS, so an existing catalog with no rows is unchanged.
CREATE TABLE IF NOT EXISTS config (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

-- One row per sequencing project (a directory in raw_sequence_data).
CREATE TABLE IF NOT EXISTS projects (
    project_id       TEXT PRIMARY KEY,   -- top-level data dir name, e.g. genohub-8459898_Vietnam
    source           TEXT,               -- 'genohub' | 'LAB'
    project_number   TEXT,               -- GenoHub project number or LAB run name
    description       TEXT,              -- free-text project description
    metadata_file     TEXT,             -- map file name, e.g. ..._mapfile.csv
    seq_data_relpath  TEXT,             -- data dir path relative to raw_sequence_data root
    seqdata_root      TEXT,             -- absolute raw_sequence_data root at ingest time
    owner_uid         INTEGER,          -- OS uid owning the project data dir
    owner_name        TEXT,             -- resolved username for owner_uid
    date_ingested     TEXT,             -- ISO date the project was ingested
    -- mapfile <-> project-folder pairing health, set by auto-discovery ingest
    metadata_status   TEXT,             -- 'ok'|'missing_mapfile'|'missing_seqdata'|'broken_mapfile'
    metadata_detail   TEXT,             -- plain-english explanation when not 'ok'
    -- data-files reciprocal check (mapfile <-> disk), refreshed by `validate`
    data_check_status    TEXT,          -- 'ok' | 'issues' | 'unchecked'
    data_check_n_missing INTEGER,       -- mapfile R1/R2 not found on disk
    data_check_n_orphan  INTEGER,       -- on-disk fastq.gz not in any mapfile
    data_check_date      TEXT,          -- ISO date of the last data-files check
    notes             TEXT
);

-- One row per sample in a project.
CREATE TABLE IF NOT EXISTS samples (
    sample_pk    INTEGER PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    sample_id    TEXT NOT NULL,          -- GenoHub/LAB sample name (map file 'ID')
    taxon        TEXT,
    uniq_id      TEXT,                   -- voucher/tissue identifier (map file 'UniqID')
    extra_json   TEXT,                   -- JSON of any extra map file columns
    flags        TEXT,                   -- ';'-joined mapfile-quality flags (NA-filled
                                         -- fields, missing reads); NULL when the row was clean
    UNIQUE (project_id, sample_id)
);
CREATE INDEX IF NOT EXISTS idx_samples_uniq_id ON samples(uniq_id);
-- Speeds the DISTINCT taxon scans in taxonomy resolve + `query taxa`.
CREATE INDEX IF NOT EXISTS idx_samples_taxon ON samples(taxon);

-- One row per FASTQ file (R1/R2 for each sample).
CREATE TABLE IF NOT EXISTS files (
    file_pk      INTEGER PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    sample_pk    INTEGER REFERENCES samples(sample_pk) ON DELETE CASCADE,
    direction    TEXT CHECK(direction IN ('R1','R2')),  -- 'R1' | 'R2' (NULL ok: diskonly files)
    filename     TEXT NOT NULL,          -- basename, e.g. sample_1.fastq.gz (may repeat across subdirs)
    rel_path     TEXT NOT NULL,          -- path relative to raw_sequence_data root; the physical file identity
    size_bytes   INTEGER,
    owner_uid    INTEGER,                -- OS uid owning the file
    owner_name   TEXT,                   -- resolved username for owner_uid
    md5          TEXT,                   -- authoritative md5 (store side once verified)
    md5_source   TEXT,                   -- 'ingest' | 'backfill'
    store_md5    TEXT,                   -- md5 from Store side
    pdrive_md5   TEXT,                   -- md5 from P-drive side
    md5_match    INTEGER CHECK(md5_match IN (0,1)),  -- 1 match, 0 mismatch, NULL not compared
    date_hashed  TEXT,
    -- gzip/FASTQ integrity check, refreshed by `integrity`
    integrity_status TEXT,               -- 'ok' | 'gzip_error' | 'format_error' | 'unchecked'
    gz_ok            INTEGER CHECK(gz_ok IN (0,1)),  -- 1 ok, 0 corrupt, NULL unchecked
    n_reads          INTEGER,            -- FASTQ read count (lines/4) when readable
    integrity_date   TEXT,              -- ISO date of the last integrity check
    -- Physical identity is the relative path, NOT the basename: two files can share
    -- a basename in different subdirs of one project (lane/run splits), and md5/
    -- integrity matching joins on rel_path. (Was UNIQUE(project_id, filename); older
    -- catalogs are rebuilt to this by db._migrate.)
    UNIQUE (project_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_files_sample ON files(sample_pk);
-- Basename lookups (mapfile R1/R2 matching, display) are non-unique now.
CREATE INDEX IF NOT EXISTS idx_files_proj_filename ON files(project_id, filename);

-- Controlled taxonomy per distinct raw Taxon string (shared across samples).
-- Populated by `seqledger.py taxonomy resolve` against a local NCBI taxdump.
CREATE TABLE IF NOT EXISTS taxa (
    taxon        TEXT PRIMARY KEY,   -- raw samples.taxon string
    clean        TEXT,               -- normalized 'Genus species'
    match_type   TEXT,               -- exact | fuzzy_species | fuzzy_genus | fuzzy_higher | unresolved | confirmed
    taxid        INTEGER,
    sci_name     TEXT,               -- NCBI scientific name of the matched node
    rank         TEXT,               -- finest resolved rank
    tax_domain   TEXT,
    tax_kingdom  TEXT,
    tax_phylum   TEXT,
    tax_class    TEXT,
    tax_order    TEXT,
    tax_family   TEXT,
    tax_genus    TEXT,
    tax_species  TEXT,
    lineage      TEXT,               -- '; '-joined ranked lineage, for display
    alternatives TEXT,               -- top fuzzy candidates
    confirmed    INTEGER DEFAULT 0 CHECK(confirmed IN (0,1)),  -- 1 once a user confirms/overrides via apply
    date_resolved TEXT,
    -- WoRMS (World Register of Marine Species) resolution, populated by
    -- `taxonomy resolve --source worms`. Parallel to the NCBI columns above and
    -- keyed on the same raw `taxon`. WoRMS's top rank is Kingdom (no domain).
    aphia_id           INTEGER,           -- WoRMS AphiaID of the accepted taxon
    worms_sci_name     TEXT,              -- accepted scientific name (valid_name)
    worms_status       TEXT,              -- accepted | unaccepted | ... (matched-name status)
    worms_match_type   TEXT,              -- exact | phonetic | near_1 | near_2 | ... | unresolved
    worms_rank         TEXT,              -- finest resolved rank
    worms_kingdom      TEXT,
    worms_phylum       TEXT,
    worms_class        TEXT,
    worms_order        TEXT,
    worms_family       TEXT,
    worms_genus        TEXT,
    worms_species      TEXT,
    worms_lineage      TEXT,              -- '; '-joined ranked lineage, for display
    worms_alternatives TEXT,             -- top candidate names
    worms_is_marine    INTEGER,           -- 1 if WoRMS flags the taxon as marine
    worms_confirmed    INTEGER DEFAULT 0 CHECK(worms_confirmed IN (0,1)), -- 1 once a user confirms/overrides via apply
    worms_date_resolved TEXT
);

-- One row per project backup location, summarizing verification state.
CREATE TABLE IF NOT EXISTS backups (
    backup_pk    INTEGER PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    location     TEXT NOT NULL,          -- e.g. 'pdrive'
    backup_date  TEXT,
    verified     INTEGER CHECK(verified IN (0,1)),  -- 1 all files matched, 0 otherwise
    n_files      INTEGER,
    n_mismatch   INTEGER,
    notes        TEXT,
    UNIQUE (project_id, location)
);

-- Per-file data-files issues from the last `validate --seqdata-root` run.
-- Rewritten per project each run; feeds the GUI drill-down.
CREATE TABLE IF NOT EXISTS data_check_issues (
    issue_pk   INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,          -- 'missing from disk' (in mapfile, not on disk) | 'missing from mapfile' (on disk, not in mapfile)
    filename   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_data_check_issues_project ON data_check_issues(project_id);

-- Log of validation runs.
CREATE TABLE IF NOT EXISTS validation_log (
    run_pk         INTEGER PRIMARY KEY,
    project_id     TEXT REFERENCES projects(project_id) ON DELETE CASCADE,
    run_date       TEXT,
    status         TEXT,                 -- 'pass' | 'warn' | 'fail'
    report_relpath TEXT
);

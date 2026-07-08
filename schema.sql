-- Ocean DNA raw sequence data catalog
-- SQLite schema. Source of truth for the catalog structure.

PRAGMA foreign_keys = ON;

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
    UNIQUE (project_id, sample_id)
);
CREATE INDEX IF NOT EXISTS idx_samples_uniq_id ON samples(uniq_id);

-- One row per FASTQ file (R1/R2 for each sample).
CREATE TABLE IF NOT EXISTS files (
    file_pk      INTEGER PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    sample_pk    INTEGER REFERENCES samples(sample_pk) ON DELETE CASCADE,
    role         TEXT,                   -- 'R1' | 'R2'
    filename     TEXT NOT NULL,          -- basename, e.g. sample_1.fastq.gz
    rel_path     TEXT,                   -- path relative to raw_sequence_data root
    size_bytes   INTEGER,
    owner_uid    INTEGER,                -- OS uid owning the file
    owner_name   TEXT,                   -- resolved username for owner_uid
    md5          TEXT,                   -- authoritative md5 (store side once verified)
    md5_source   TEXT,                   -- 'ingest' | 'backfill'
    store_md5    TEXT,                   -- md5 from Store side
    pdrive_md5   TEXT,                   -- md5 from P-drive side
    md5_match    INTEGER,                -- 1 match, 0 mismatch, NULL not compared
    date_hashed  TEXT,
    UNIQUE (project_id, filename)
);
CREATE INDEX IF NOT EXISTS idx_files_sample ON files(sample_pk);

-- One row per project backup location, summarizing verification state.
CREATE TABLE IF NOT EXISTS backups (
    backup_pk    INTEGER PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    location     TEXT NOT NULL,          -- e.g. 'pdrive'
    backup_date  TEXT,
    verified     INTEGER,                -- 1 all files matched, 0 otherwise
    n_files      INTEGER,
    n_mismatch   INTEGER,
    notes        TEXT,
    UNIQUE (project_id, location)
);

-- Log of validation runs.
CREATE TABLE IF NOT EXISTS validation_log (
    run_pk         INTEGER PRIMARY KEY,
    project_id     TEXT REFERENCES projects(project_id) ON DELETE CASCADE,
    run_date       TEXT,
    status         TEXT,                 -- 'pass' | 'warn' | 'fail'
    report_relpath TEXT
);

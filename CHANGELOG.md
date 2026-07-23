# Changelog

All notable changes to seqledger are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

A hardening pass across the CLI, ingest/integrity pipeline, and browse GUI, driven
by an adversarial review. Highlights (specifics may still shift):

### Added

- `LICENSE` file (MIT) at the repository root, matching the `pyproject.toml`
  license declaration.
- This `CHANGELOG.md`.

### Changed

- **File identity is now the relative path, not the basename.** The `files`
  table is keyed on `UNIQUE(project_id, rel_path)` (was `project_id, filename`),
  so two FASTQ files that share a basename in different subdirectories of one
  project (lane/run splits) are both cataloged, and md5 verification joins on the
  full path instead of cross-contaminating same-named files. Existing catalogs
  are rebuilt to the new key automatically on the next `init-db` (the file table
  is recreated in place, preserving all rows; **back up the catalog first**, as
  the README already advises for `init-db`).
- Documentation corrected to describe the integrity check accurately: it
  decompresses every byte (equivalent to `gzip -t`, catching truncation, CRC
  errors, and bit-rot) and verifies the total line count is a multiple of 4, but
  does **not** validate per-record `@`/`+` framing.
- Documentation clarifies that the map file's required columns are a fixed set
  (`ID`, `R1`, `R2`, `Taxon`, `UniqID`/`UniqueID`), matched case-insensitively.

### Fixed

- `ingest --prune` / `--prune-projects` safety guards, so a missing or unmounted
  root cannot wipe the catalog.
- SQLite-lock resilience in the GUI when reading a catalog that is being written.
- Checksum and integrity correctness fixes (comparison and per-file result
  handling).
- Taxonomy resolution hardening for edge-case and ambiguous names.
- CSV-injection sanitization on exported map files and tables.

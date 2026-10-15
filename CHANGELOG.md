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
- The GUI now binds `127.0.0.1` only (locally and under `--qsub`), so other
  cluster users can no longer reach it over the network. The printed tunnel is
  now `ssh -N -L <port>:127.0.0.1:<port> -J <you>@<login_host> <you>@<node>`,
  and off the cluster only the `http://localhost:<port>` link is printed.

### Fixed

- New catalogs are created with mode `0600` (owner-only); existing catalogs keep
  their permissions.
- The read-only connection URI now escapes the catalog path, so a `?` or `#` in
  the path can no longer drop `mode=ro`; the GUI connection is also query-only.
- The CLI now waits up to 60 s for a locked catalog (was 5 s), and a local
  `integrity` run commits at least every 30 s so it no longer starves other
  writers. The GUI waits about 15 s on a locked catalog (was about 50 s) and shows
  a "retrying" message while it waits.
- `gui --qsub` reports failure (with the job log path) instead of printing a
  tunnel when the Streamlit server never started.

- `ingest --prune` / `--prune-projects` safety guards, so a missing or unmounted
  root cannot wipe the catalog.
- SQLite-lock resilience in the GUI when reading a catalog that is being written.
- Checksum and integrity correctness fixes (comparison and per-file result
  handling).
- Taxonomy resolution hardening for edge-case and ambiguous names.
- CSV-injection sanitization on exported map files and tables.

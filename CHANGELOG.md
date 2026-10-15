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

- Config values and filenames are now shell-quoted in generated qsub and rclone
  scripts, and newlines, carriage returns, and NUL are rejected in config values
  and FASTQ filenames, closing command injection via the catalog or a mapfile.
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
- GUI CSV exports were not sanitized against spreadsheet formula injection on pandas 3.
- Checksum and integrity correctness fixes (comparison and per-file result
  handling).
- Taxonomy resolution hardening for edge-case and ambiguous names.
- CSV-injection sanitization on exported map files and tables.
- `checksums` no longer resets backup verification for projects absent from the
  md5 listing (use `--full` for a catalog-wide reset), and aborts without changes
  on a truncated listing unless `--force`.
- `checksums` matches uppercase md5 hex (PowerShell `Get-FileHash`) and paths
  with a leading `./` or backslashes.
- `integrity` re-checks a file whose size changed since its last check; a re-ingest
  that sees a new size clears the old integrity result and backup match.
- `integrity --batch` no longer reuses results from a completed run, and `--collect`
  only writes to the result file's own project and never overwrites a newer result.
- Files that cannot be read (permissions, I/O errors) are reported `unchecked`
  instead of corrupt.

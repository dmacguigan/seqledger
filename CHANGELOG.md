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
- CLI commands now exit non-zero on failures and refusals (ingest FAIL, integrity
  gzip/format errors, checksums mismatches, validate issues: 1; refused prune or
  failed `integrity --batch` submission: 2), so cron jobs can detect problems.
- `--db` is accepted after the subcommand too (e.g. `seqledger query summary --db x.db`).
- `init-db --show` is read-only: it prints the config without creating, migrating,
  or changing the catalog.
- Grab & Go: the copy script is shown as a short preview (first 20 files); the
  full script is in the download. The job log and download names carry a
  timestamp, rclone no longer floods the log with `--progress`, and the job exits
  non-zero if any copy fails.
- Grab & Go: a `files_ok` column flags corrupt, empty, missing, or unchecked
  files per sample, with a warning and an option (on by default) to leave
  problem files out of the copy job and MitoPilot map.
- `gui --qsub` now requests 6 GB by default (was 2 GB, which a large catalog
  exceeds). Raise `--mem` if the job dies at once.
- `gui --qsub` deletes its own job, log, and ready files in `~/.seqledger_gui/`
  that are older than 7 days.
- Taxonomy review CSVs pre-fill `confirmed_taxid` / `confirmed_aphia_id` only for
  exact matches, so applying an untouched CSV no longer confirms fuzzy guesses.
- `taxonomy resolve` no longer overwrites an existing review CSV: it writes
  `<name>_YYYY-MM-DD.csv` (then `_2`, `_3`) and prints the path. No review CSV is
  written when nothing was resolved.
- `--redo` help and docs now say confirmed taxa are kept (re-resolved for the
  review CSV only, not overwritten).

### Fixed

- GUI: a selected row in the Samples, Projects, or Files table is cleared when the
  filters change, instead of crashing or showing the wrong record.
- GUI: a project with no stored mapfile detail shows the standard explanation
  instead of "nan".
- GUI: an older catalog copy missing tables or columns now shows a clear "run
  init-db and re-sync" message instead of a database error.
- GUI: match-type legends cover every stored value (WoRMS `review_*` types, NCBI
  `ambiguous`), and the Samples table shows the same readable labels as the charts.
- GUI: the Taxonomy view explains how to install plotly when it is missing, and
  says "No samples in this catalog yet." on an empty catalog.

- The `backup_location` config is now honored by `checksums`, `query unbacked`,
  and the GUI backup column (it was hardcoded to `pdrive`).
- Ctrl-C during a local `integrity` run now stops at once and saves the results
  checked so far.
- `validate` marks a project `unchecked` (with "data folder not found") when its
  data folder is gone, instead of keeping a stale `ok`. An unmounted root is
  still skipped.
- `validate` counts cataloged files not linked to any sample: for a project with
  a mapfile they are reported as missing from the mapfile; for a project with no
  mapfile the count is printed.
- `integrity` no longer flags a file whose last read is empty as a format error.

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
- MitoPilot map R1/R2 are now paths relative to the copy destination (matching
  the copied layout), not bare filenames, so `data_path = DEST` works.
- Lane-split samples are flagged in Grab & Go (MitoPilot gets only the first file
  per direction), and the Samples R1/R2 path columns list every lane file.

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
- Commands other than `init-db` no longer create an empty catalog at a mistyped
  `--db` path; they stop with a message pointing at `--db` / `SEQLEDGER_DB`.
- `ingest --prune-projects` prints the real reason it refused to prune.
- `integrity --batch` reports the configured I/O queue, not a hardcoded `lTIO.sq`.
- An offline first `ingest` no longer prints a traceback when the NCBI taxdump
  download fails; the taxonomy step is skipped with a message.
- Re-ingesting a project whose mapfile is missing, broken, or has no usable rows
  no longer unlinks its files from their samples; existing file rows keep their
  sample and R1/R2 direction, and the project keeps its mapfile name.
- A FASTQ file moved to another subdirectory keeps its catalog row and history
  (md5 and integrity results) instead of leaving a stale duplicate row.
- `ingest --prune` deletes file rows by relative path and reports the true number
  of rows removed. It refuses to drop more than half of a project's samples (when
  the project has more than 2), which usually means a truncated mapfile.
- Project folders or roots whose names contain glob characters (e.g. `[1]`) now
  have their files found.
- A mapfile row with more cells than the header (often an unquoted comma) is
  flagged with a warning instead of silently shifting columns.
- `taxonomy apply` reads review CSVs saved by Excel (UTF-8 with BOM, or cp1252),
  and stops with a clear error when given the other source's review CSV.
- A taxon first resolved by WoRMS is now still resolved against NCBI and listed
  by `query taxa` until it is.
- A genus name shared by two kingdoms is flagged `ambiguous` on the fuzzy species
  path too, instead of silently picking one.

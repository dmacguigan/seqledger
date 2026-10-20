# Changelog

All notable changes to seqledger are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] - 2026-10-20

### Added

- GUI: 'Filter columns' bar on the Grab & Go table, like the other tables.
- Dismissable GUI issues. Any user can dismiss a mapfile issue, a data-files
  problem, a sample's flags, or a file's problem from Projects, Samples, and
  Files. Dismissals are shared and only hide the issue; the catalog is not
  changed. Each issue gets a boxed panel under the table with a **Reason
  (optional)** box and a Dismiss button; once dismissed, the panel shows who,
  when, and why with a **Restore** button. Cells read `OK (+n dismissed)`, or
  the live text plus `(+n dismissed)`; **Show dismissed issues** lists dismissed
  rows in the Projects issues table, and files with only dismissed problems are
  included in Grab & Go copies. Dismissals live in a hidden
  `.<catalog name>_dismissed.tsv` next to the catalog.
- Status cells are tinted: vermillion for errors, orange for warnings, sky blue
  for dismissed, grey for not yet checked.
- `init-db` records the dismissals log path in the config key `dismiss_log`, so
  Scratch copies of the catalog use the master's log. Back up the catalog, then
  re-run `init-db` on the master so copies find the log.

### Changed

- The Projects "Data-files issues" table also lists md5 mismatches, and the
  integrity rows show each file's relative path.
- Minimum pandas is now 2.1 (was 2.0).

## [0.3.0] - 2026-10-19

### Added

- A mapfile `R1`/`R2` name found in several subfolders (one library over
  several runs) now links every copy to that sample instead of keeping the last
  one and reporting the rest as "on disk, not in mapfile". To pick one copy,
  name it by its path under the project folder (`Run1/x_R1.fastq.gz`).

## [0.2.1] - 2026-10-19

### Fixed

- GUI: samples flagged `extra_cells` were not tinted as warnings.

- `init-db` now also refuses a catalog whose `files` table still has the old
  basename unique key (it only checked for missing columns), so `ingest` no
  longer fails with "ON CONFLICT clause does not match". Fix: run `init-db` once
  with the 0.1.0 code (`git checkout f3174b6`), then again with the current code.

## [0.2.0] - 2026-10-19

### Added

- `seqledger` with no subcommand in a terminal opens a text menu for common
  tasks; each step prints the equivalent command. Off a terminal (cron, pipes)
  it still prints usage and exits 2.
- `--version` flag.
- Plain `ingest` lists projects gone from both disk and the mapfile folder, so
  `--prune-projects` has a preview before it deletes them.
- `docs/` user guide, admin guide, reference pages, and glossary; README is now
  a short landing page.
- `LICENSE` (MIT), this `CHANGELOG.md`, `CITATION.cff`, and `CONTRIBUTING.md`.

### Changed

- New `-v` / `-vv` flags (before or after the subcommand): `-v` lists every
  project, `-vv` every per-file warning, sorted by path.
- By default `ingest`, `checksums`, `validate`, and `integrity` print one short
  summary: counts, a Problems list capped at 10 projects, and one "Next:" step.
  `integrity` names damaged files and `checksums` mismatched ones (up to 10).
- Progress counters and the "using configured root" note go to stderr, progress
  only on a terminal. `checksums` warnings are sorted, a missing mapfile is
  reported once, and "next" hints show only when there is something to do.
- Plainer wording: "on disk, not in mapfile" (was orphan), "backup not yet
  verified", "bad FASTQ line count", "R1 and R2 read counts differ".
  `query -h` explains the column names.
- `init-db` keeps only `--name`, `--seqdata-root`, `--metadata-root`, `--set`,
  and `--show`; other settings use `--set KEY=VALUE` (e.g. `--set
  conda_env=myenv`). `init-db -h` lists the keys.
- `init-db --show` is read-only: it never creates or changes the catalog.
- File identity is the relative path, not the basename (`UNIQUE(project_id,
  rel_path)`), so same-named FASTQ files in different subdirectories are both
  cataloged and md5 checks no longer mix them up.
- Taxonomy review CSVs pre-fill `confirmed_taxid` / `confirmed_aphia_id` only
  for exact matches, so applying an untouched CSV no longer confirms fuzzy guesses.
- `taxonomy resolve` never overwrites a review CSV: it writes
  `<name>_YYYY-MM-DD.csv` (then `_2`, `_3`), and none when nothing was resolved.
- `--redo` help and docs say confirmed taxa are kept (re-resolved for the review
  CSV only).
- CLI commands exit non-zero on failures (1) and on refused prunes or failed
  `integrity --batch` submissions (2), so cron jobs can detect problems.
- `--db` is accepted after the subcommand too.
- The GUI binds `127.0.0.1` only, locally and under `--qsub`. The printed tunnel
  is `ssh -N -L <port>:127.0.0.1:<port> -J <you>@<login_host> <you>@<node>`.
- The GUI waits up to 15 s on a locked catalog, then shows "Catalog is being
  updated, retry shortly." instead of retrying itself.
- `gui --qsub` requests 6 GB by default (was 2 GB) and deletes its own job, log,
  and ready files in `~/.seqledger_gui/` older than 7 days.
- Grab & Go: a `files_ok` column flags corrupt, empty, missing, or unchecked files,
  with an option (on by default) to leave them out of the copy job and MitoPilot map.
- Grab & Go: the copy script preview shows the first 20 files; job log and
  download names are timestamped; rclone drops `--progress`; the job exits
  non-zero if any copy fails.
- Docs describe the integrity check accurately (full decompress plus line count
  a multiple of 4; no per-record framing check) and the fixed map file columns.
- Internal: NCBI and WoRMS taxonomy share one set of scope, review-CSV, and apply
  helpers.
- Ctrl-C stops any command with a one-line "interrupted" and exit code 130
  (no traceback).

### Removed

- Legacy schema migrations. A catalog must have run `init-db` with 0.1.0 before
  upgrading; older catalogs are refused with a message. **Back up the catalog
  before `init-db`.**
- The two-column map file mode of `ingest` (positional `map_file`). Ingest always
  auto-discovers from `--seqdata-root` and `--metadata-root`.
- GUI fallbacks for catalog copies missing newer columns or the `config` table,
  and the legacy `missing`/`orphan` issue kinds.
- `requirements.txt`; use `pip install -e .[test]` or `environment.yml`.

### Fixed

- `query` with no results prints "(none)" to stderr, so stdout holds only the table.
- Commands other than `init-db` no longer create an empty catalog at a mistyped
  `--db` path; they point at `--db` / `SEQLEDGER_DB`.
- `ingest --prune` / `--prune-projects` guard against a missing or unmounted root,
  delete file rows by relative path, and refuse to drop over half of a project's
  samples (projects with more than 2).
- Prune refusals give the real reason and say to check the mount, mapfile folder,
  or mapfile (no more "pass force=True").
- Re-ingesting a project whose mapfile is missing or broken keeps its file rows
  linked to their samples, R1/R2 direction, and mapfile name.
- A FASTQ file moved to another subdirectory keeps its catalog row and md5 and
  integrity history.
- Project folders or roots with glob characters (e.g. `[1]`) have their files found.
- A mapfile row with more cells than the header is flagged instead of silently
  shifting columns.
- An offline first `ingest` skips the taxonomy step with a message instead of a
  traceback when the NCBI taxdump download fails.
- `checksums` no longer resets backup verification for projects absent from the
  md5 listing (use `--full`), and aborts on a truncated listing unless `--force`.
- `checksums` matches uppercase md5 hex (PowerShell `Get-FileHash`) and paths with
  a leading `./` or backslashes.
- Checksum and integrity correctness fixes (comparison and per-file results).
- The `backup_location` config is honored by `checksums`, `query unbacked`, and
  the GUI backup column (was hardcoded to `pdrive`).
- `integrity` re-checks a file whose size changed; a re-ingest that sees a new
  size clears the old integrity result and backup match.
- `integrity` no longer flags a file whose last read is empty as a format error.
- Unreadable files (permissions, I/O errors) are reported `unchecked`, not corrupt.
- Ctrl-C during a local `integrity` run stops at once and saves results so far.
- `integrity --batch` no longer reuses results from a completed run, `--collect`
  writes only to the result file's project and never over a newer result, and the
  configured I/O queue is reported (not a hardcoded `lTIO.sq`).
- `validate` marks a project `unchecked` when its data folder is gone (an
  unmounted root is still skipped).
- `validate` counts cataloged files not linked to any sample.
- `taxonomy apply` reads Excel-saved review CSVs (UTF-8 with BOM, or cp1252) and
  stops with a clear error on the other source's review CSV.
- A taxon first resolved by WoRMS is still resolved against NCBI and listed by
  `query taxa` until it is.
- Taxonomy edge cases: a genus shared by two kingdoms is flagged `ambiguous` on
  the fuzzy species path too, instead of silently picking one.
- MitoPilot map R1/R2 are paths relative to the copy destination, so
  `data_path = DEST` works.
- Lane-split samples are flagged in Grab & Go (MitoPilot gets only the first file
  per direction), and the Samples R1/R2 path columns list every lane file.
- `gui --qsub` reports failure with the job log path when Streamlit never started.
- GUI: a selected table row is cleared when filters change, instead of crashing
  or showing the wrong record.
- GUI: an older catalog copy missing tables or columns shows a "run init-db and
  re-sync" message instead of a database error.
- GUI: a project with no stored mapfile detail shows the standard explanation
  instead of "nan".
- GUI: match-type legends cover every stored value, and the Samples table uses
  the same labels as the charts.
- GUI: the Taxonomy view explains how to install plotly when missing, and says
  "No samples in this catalog yet." on an empty catalog.
- Security: config values and filenames are shell-quoted in generated qsub and
  rclone scripts; newlines, carriage returns, and NUL are rejected in them.
- Security: exported CSVs and map files are sanitized against spreadsheet formula
  injection (also on pandas 3).
- Security: new catalogs are created mode `0600`; the read-only connection URI
  escapes the catalog path, and the GUI connection is query-only.
- The CLI waits up to 60 s for a locked catalog (was 5 s), and a local
  `integrity` run commits at least every 30 s so it does not starve other writers.
- `init-db --set` refuses unknown config keys and lists the known ones.
- `--debug` works after the subcommand, like `--db` and `-v`.

[Unreleased]: https://github.com/dmacguigan/seqledger/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/dmacguigan/seqledger/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/dmacguigan/seqledger/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/dmacguigan/seqledger/compare/f3174b6...v0.2.0

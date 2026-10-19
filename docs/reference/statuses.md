# Status reference

Every status value seqledger stores, which command sets it, and how the command
line and the GUI word it.

Contents

- [Overview](#overview)
- [Projects: mapfile status](#projects-mapfile-status)
- [Projects: data-files check](#projects-data-files-check)
- [Backup checksum](#backup-checksum)
- [File integrity](#file-integrity)
- [Taxonomy match types](#taxonomy-match-types)
- [Integrity run log](#integrity-run-log)
- [Grab & Go files_ok](#grab--go-files_ok)

## Overview

| Field | Values | Set by | GUI column |
|---|---|---|---|
| `projects.metadata_status` | ok, flagged, missing_mapfile, missing_seqdata, broken_mapfile, invalid_mapfile | `ingest` | Projects `mapfile` |
| `projects.data_check_status` | ok, issues, unchecked | `validate`, `ingest` | Projects `data_files` |
| `files.md5_match` | 1, 0, NULL | `checksums` (reset to NULL by `ingest` when a file's size changes) | Files `backup`, Projects `checksum` |
| `backups.verified` | 1, 0 | `checksums` | Samples `backup` |
| `files.integrity_status` | NULL, ok, gzip_error, format_error, unchecked | `integrity` (local or `--collect`); reset to NULL by `ingest` when a file's size changes | Files `integrity`, Projects `integrity` |
| `samples.flags` | see [mapfile flags](mapfile.md#flag-tokens) | `ingest` | Samples `flags` |
| `taxa.match_type` | exact, fuzzy_species, fuzzy_genus, fuzzy_higher, ambiguous, unresolved, confirmed | `taxonomy resolve`, `taxonomy apply`, `ingest` | Samples `NCBI_match` |
| `taxa.worms_match_type` | exact, near_1, review_phonetic, review_near_2, review_near_3, review_match_quarantine, unresolved, confirmed | `taxonomy resolve --source worms`, `taxonomy apply --source worms` | Samples `WoRMS_match` |
| `validation_log.status` | pass, warn, fail | `integrity` | not shown |

## Projects: mapfile status

`projects.metadata_status`, with a sentence in `projects.metadata_detail`.

| Value | GUI label | `ingest` prints | Meaning |
|---|---|---|---|
| `ok` | OK | (nothing) | Folder and mapfile present, every row clean |
| `flagged` | flagged rows | `N mapfile warnings` or the warning itself | Rows repaired (NA-filled) or skipped; samples carry flags |
| `missing_mapfile` | no mapfile | `no mapfile for this project folder; files cataloged without sample metadata` | Files cataloged from disk, no samples |
| `missing_seqdata` | no data folder | `no project folder on disk for this mapfile` | Samples loaded, no files on disk |
| `broken_mapfile` | broken mapfile | `rejected; mapfile header is malformed; sample metadata skipped` | Header does not start with ID,R1,R2,Taxon,UniqID; files cataloged, no samples |
| `invalid_mapfile` | no usable rows | `rejected;` followed by the skipped rows | Header fine, every row skipped; files cataloged, no samples |
| NULL | (blank) | | Not set |

`broken_mapfile` and `invalid_mapfile` make `ingest` exit 1.

## Projects: data-files check

`projects.data_check_status`, with counts in `data_check_n_missing` and
`data_check_n_orphan` and one row per problem file in `data_check_issues`.
The CLI (`ingest`, `validate`) and the GUI use the same words.

| Value | Label | Meaning |
|---|---|---|
| `ok` | OK | Mapfile and disk agree |
| `ok` | OK, N file(s) not linked to any sample (no mapfile) | A project with no usable mapfile: its files are there but have no sample |
| `issues` | N file(s) in mapfile not on disk, M file(s) on disk, not in mapfile | Either or both counts |
| `unchecked` | unchecked | Never checked, or no root known |
| `unchecked` | unchecked (data folder not found: `<path>`) | The root exists but the project folder does not |

`query summary` shows the raw value. In the GUI drill-down (click a project),
each problem file is listed with kind "missing from disk" or "missing from
mapfile".

## Backup checksum

Per file, `files.md5_match`:

| Value | GUI Files `backup` | Meaning |
|---|---|---|
| 1 | Verified | Store and P-drive md5 are equal |
| 0 | Mismatch | They differ |
| NULL | Not compared | Not in both listings yet |

Per project (computed from the files; same words in the CLI and the GUI Projects
`checksum` column):

| Label | Meaning |
|---|---|
| backup verified | Every file matched |
| backup not yet verified (N file(s)) | No mismatch, but N files not compared |
| N file(s) differ between Store and P-drive | At least one mismatch |
| no files | Project has no files |

`backups.verified` (one row per project, for the `backup_location` config value)
is 1 only when every file matched. The GUI Samples `backup` column and its filter
show it as Verified / Not verified; it is the project's state, repeated on each
sample. `query unbacked` lists projects where it is not 1.

`checksums` per-file warnings: "differ between Store and P-drive", "missing from
the Store listing", "missing from the P-drive listing", "in neither listing
(backup not yet verified)", "on disk, not in mapfile".

## File integrity

Per file, `files.integrity_status`, with `gz_ok` (1 ok, 0 damaged, NULL not
checked), `n_reads`, and `integrity_date`:

| Value | CLI label | GUI Files `integrity` | Meaning |
|---|---|---|---|
| NULL | not checked (not found on disk or unreadable) | unchecked | Never checked |
| `ok` | OK | ok | Read to the end; line count a multiple of 4 |
| `gzip_error` | gzip error | gzip_error | Not valid gzip, truncated, or CRC failure |
| `format_error` | bad FASTQ line count | format_error | Decompresses, but the line count is not a multiple of 4 |
| `unchecked` | not checked (not found on disk or unreadable) | unchecked | File missing, unreadable, or no root known |

NULL and `unchecked` look the same in both the CLI and the GUI. Use SQL to tell
them apart ([data model](data-model.md#example-queries)).

Per project (GUI Projects `integrity` column):

| Label | Meaning |
|---|---|
| verified | Every file ok |
| N corrupt | N files with gzip_error or format_error |
| N empty | N files ok but with 0 reads |
| incomplete (N unchecked) | Some files not checked yet |
| unchecked | No file checked yet |
| no files | Project has no files |

The CLI also reports "empty file(s)" and "sample(s) where R1 and R2 read counts
differ". The GUI drill-down lists files as "gzip error", "format error", or
"empty (0 reads)".

## Taxonomy match types

NCBI, `taxa.match_type`:

| Value | GUI label | Meaning |
|---|---|---|
| `exact` | exact | Name found as is |
| `fuzzy_species` | fuzzy (species) | Close spelling match at species level |
| `fuzzy_genus` | fuzzy (genus) | Matched only the genus |
| `fuzzy_higher` | fuzzy (higher) | Matched only a rank above genus |
| `ambiguous` | ambiguous (homonym) | The genus name exists in more than one kingdom |
| `unresolved` | unresolved | No match |
| `confirmed` | confirmed | Set by `taxonomy apply` from the review CSV; never overwritten by a resolve |

WoRMS, `taxa.worms_match_type`:

| Value | GUI label | AphiaID stored? |
|---|---|---|
| `exact` | exact | yes |
| `near_1` | near (1 edit) | yes |
| `review_phonetic` | review: phonetic (sounds-like) | no, review it |
| `review_near_2` | review: near (2 edits) | no, review it |
| `review_near_3` | review: near (3 edits) | no, review it |
| `review_match_quarantine` | review: quarantined | no, review it |
| `unresolved` | unresolved | no |
| `confirmed` | confirmed | yes, set by `taxonomy apply --source worms` |

`taxa.confirmed` / `taxa.worms_confirmed` are 1 once a row has been applied.

## Integrity run log

`validation_log.status`, one row per project each time `integrity` runs locally or
`--collect` merges results:

| Value | Meaning |
|---|---|
| `pass` | All files ok, R1/R2 counts agree |
| `warn` | Unchecked files, empty files, or R1/R2 count differences |
| `fail` | At least one gzip or format error |

`validate` does not write this table.

## Grab & Go files_ok

Computed per sample in the Grab & Go table from its files:

| Label | Meaning |
|---|---|
| ok | All files ok |
| N corrupt | gzip or format error |
| N empty | 0 reads |
| N missing | Listed in the mapfile, not on disk |
| unchecked | At least one file not checked yet |
| no files | No files cataloged |

Several can appear together, for example `1 corrupt, unchecked`.

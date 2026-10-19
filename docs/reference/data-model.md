# Data model

The catalog's tables and columns, and example SQL for questions the CLI and GUI
do not answer. The source of truth is
[`seqledger/schema.sql`](../../seqledger/schema.sql).

Contents

- [Overview](#overview)
- [projects](#projects)
- [samples](#samples)
- [files](#files)
- [taxa](#taxa)
- [backups](#backups)
- [data_check_issues](#data_check_issues)
- [validation_log](#validation_log)
- [config](#config)
- [Example queries](#example-queries)

## Overview

```
config                    key/value settings

projects                  one per project folder
  |-- samples             one per mapfile row       (samples.taxon joins taxa.taxon)
  |     \-- files         one per FASTQ             (files.sample_pk, NULL if no sample)
  |-- files               (files.project_id)
  |-- backups             one per project and backup location
  |-- data_check_issues   one per problem file from the last data-files check
  \-- validation_log      one per project per integrity run

taxa                      one per distinct Taxon string
```

Deleting a project deletes its samples, files, backups, issues, and log rows
(foreign keys with `ON DELETE CASCADE`; turn them on with
`PRAGMA foreign_keys=ON` in the `sqlite3` shell).

Paths: `files.rel_path` is relative to the `raw_sequence_data` root and starts
with the project folder. The full path is `projects.seqdata_root || '/' ||
files.rel_path`.

## projects

| Column | Meaning |
|---|---|
| `project_id` | Primary key; the top-level folder name |
| `source`, `project_number`, `description` | Parsed from the folder name ([naming](mapfile.md#project-folder-names)) |
| `metadata_file` | Mapfile name |
| `seq_data_relpath` | Folder path relative to the root |
| `seqdata_root` | Absolute root at ingest time |
| `owner_uid`, `owner_name` | Owner of the project folder on disk (personal data) |
| `date_ingested` | ISO date of the last ingest |
| `metadata_status`, `metadata_detail` | Mapfile status and explanation ([statuses](statuses.md#projects-mapfile-status)) |
| `data_check_status`, `data_check_n_missing`, `data_check_n_orphan`, `data_check_date` | Last data-files check ([statuses](statuses.md#projects-data-files-check)) |
| `notes` | Unused |

## samples

| Column | Meaning |
|---|---|
| `sample_pk` | Primary key |
| `project_id` | Project |
| `sample_id` | Mapfile `ID`; unique within a project |
| `taxon` | Mapfile `Taxon` as typed (`NA` if empty) |
| `uniq_id` | Mapfile `UniqID` (`NA` if empty) |
| `extra_json` | Extra mapfile columns as JSON, or NULL |
| `flags` | `;`-separated [flag tokens](mapfile.md#flag-tokens), or NULL |

## files

| Column | Meaning |
|---|---|
| `file_pk` | Primary key |
| `project_id` | Project |
| `sample_pk` | Sample, or NULL for files with no mapfile row |
| `direction` | `R1`, `R2`, or NULL |
| `filename` | Base name |
| `rel_path` | Path relative to the root; unique within a project |
| `size_bytes`, `owner_uid`, `owner_name` | From disk at ingest |
| `md5` | Store md5 once loaded |
| `md5_source` | `ingest` or `backfill` (a label) |
| `store_md5`, `pdrive_md5`, `md5_match`, `date_hashed` | Last `checksums` comparison |
| `integrity_status`, `gz_ok`, `n_reads`, `integrity_date` | Last integrity check ([statuses](statuses.md#file-integrity)) |

## taxa

Keyed on the raw `taxon` string, shared by every sample that uses it.

| Column | Meaning |
|---|---|
| `taxon` | Primary key, same as `samples.taxon` |
| `clean` | Normalized name used for matching |
| `match_type`, `taxid`, `sci_name`, `rank` | NCBI result |
| `tax_domain`, `tax_kingdom`, `tax_phylum`, `tax_class`, `tax_order`, `tax_family`, `tax_genus`, `tax_species` | NCBI lineage |
| `lineage`, `alternatives` | NCBI lineage as text; top fuzzy candidates |
| `confirmed`, `date_resolved` | 1 after `taxonomy apply`; date of last resolve |
| `aphia_id`, `worms_sci_name`, `worms_status`, `worms_match_type`, `worms_rank` | WoRMS result (accepted name) |
| `worms_kingdom`, `worms_phylum`, `worms_class`, `worms_order`, `worms_family`, `worms_genus`, `worms_species` | WoRMS lineage (no domain rank) |
| `worms_lineage`, `worms_alternatives`, `worms_is_marine` | WoRMS lineage as text; candidate names; 1 if marine |
| `worms_confirmed`, `worms_date_resolved` | 1 after `taxonomy apply --source worms`; date |

## backups

| Column | Meaning |
|---|---|
| `backup_pk` | Primary key |
| `project_id`, `location` | One row per project and location (`backup_location` config, default `pdrive`) |
| `backup_date` | Date of the last `checksums` run that touched it |
| `verified` | 1 if every file matched, else 0 |
| `n_files`, `n_mismatch` | Counts at that run |
| `notes` | Unused |

## data_check_issues

Rewritten per project by every data-files check.

| Column | Meaning |
|---|---|
| `issue_pk` | Primary key |
| `project_id` | Project |
| `kind` | `missing from disk` or `missing from mapfile` |
| `filename` | The file's relative path |

## validation_log

| Column | Meaning |
|---|---|
| `run_pk` | Primary key |
| `project_id` | Project |
| `run_date` | ISO date |
| `status` | `pass`, `warn`, `fail` ([statuses](statuses.md#integrity-run-log)) |
| `report_relpath` | Unused |

## config

| Column | Meaning |
|---|---|
| `key`, `value`, `updated_at` | One row per key set with `init-db` ([config](config.md)) |

## Example queries

Open the catalog read-only so a typo cannot change it:

```bash
sqlite3 -readonly -header -separator "$(printf '\t')" /path/to/catalog.db
```

Full paths of every file for one UniqID:

```sql
SELECT s.project_id, s.sample_id, f.direction,
       p.seqdata_root || '/' || f.rel_path AS path
FROM samples s
JOIN files f ON f.sample_pk = s.sample_pk
JOIN projects p ON p.project_id = s.project_id
WHERE s.uniq_id = 'USNM 477715'
ORDER BY s.project_id, f.direction;
```

Damaged files:

```sql
SELECT project_id, rel_path, integrity_status, integrity_date
FROM files
WHERE integrity_status IN ('gzip_error', 'format_error')
ORDER BY project_id, rel_path;
```

Files never checked, versus checked but not found:

```sql
SELECT project_id,
       SUM(integrity_status IS NULL) AS never_checked,
       SUM(integrity_status = 'unchecked') AS not_found_or_unreadable
FROM files GROUP BY project_id;
```

Projects with mapfile problems, with the explanation:

```sql
SELECT project_id, metadata_status, metadata_detail
FROM projects
WHERE metadata_status <> 'ok'
ORDER BY metadata_status, project_id;
```

Data size per project, in GB:

```sql
SELECT project_id, COUNT(*) AS n_files,
       ROUND(SUM(size_bytes) / 1073741824.0, 1) AS gb
FROM files GROUP BY project_id ORDER BY gb DESC;
```

Samples per NCBI family:

```sql
SELECT COALESCE(t.tax_family, 'unknown') AS family, COUNT(*) AS n_samples
FROM samples s LEFT JOIN taxa t ON t.taxon = s.taxon
GROUP BY family ORDER BY n_samples DESC LIMIT 20;
```

An extra mapfile column (here `Collector`):

```sql
SELECT project_id, sample_id, json_extract(extra_json, '$.Collector') AS collector
FROM samples
WHERE json_extract(extra_json, '$.Collector') <> '';
```

Integrity history for one project:

```sql
SELECT run_date, status FROM validation_log
WHERE project_id = 'genohub-2054899_OKEX01' ORDER BY run_pk DESC;
```

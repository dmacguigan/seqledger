# Mapfile reference

The exact format of a project's mapfile, and what `ingest` does with each part.

Contents

- [Example](#example)
- [File name and location](#file-name-and-location)
- [Columns](#columns)
- [Row rules](#row-rules)
- [How R1 and R2 are matched to files](#how-r1-and-r2-are-matched-to-files)
- [Encodings and CSV format](#encodings-and-csv-format)
- [What ingest records](#what-ingest-records)
- [Flag tokens](#flag-tokens)
- [Project folder names](#project-folder-names)

## Example

`raw_sequence_metadata/genohub-2054899_OKEX01_mapfile.csv`:

```
ID,R1,R2,Taxon,UniqID,Collector,Station
S01,S01_L001_R1_001.fastq.gz,S01_L001_R2_001.fastq.gz,Gadus morhua,USNM 477715,Smith,ST12
S02,S02_L001_R1_001.fastq.gz,S02_L001_R2_001.fastq.gz,Urophycis sp.,USNM 480112,Lee,ST12
NEG1,NEG1_L001_R1_001.fastq.gz,NEG1_L001_R2_001.fastq.gz,,,Lee,
```

for a project folder `raw_sequence_data/genohub-2054899_OKEX01/` that holds those
six FASTQs at any depth. Row `NEG1` loads with `Taxon` and `UniqID` set to `NA`
and the flags `na_taxon;na_uniqid`, so the project shows "flagged rows".

## File name and location

- Name: `<project>_mapfile.csv`, where `<project>` is exactly the project folder
  name (same spelling and case).
- Location: directly in the metadata folder (`metadata_root` or
  `--metadata-root`). Subfolders are not searched.
- A mapfile with no matching folder is still loaded (status "no data folder").
  A folder with no mapfile still has its files cataloged (status "no mapfile").

## Columns

The first five columns are positional and required, in this order:

| # | Header | Content |
|---|---|---|
| 1 | `ID` | Sample ID, unique within the mapfile |
| 2 | `R1` | File name of the read 1 FASTQ |
| 3 | `R2` | File name of the read 2 FASTQ |
| 4 | `Taxon` | Free-text taxon name, as precise as you know it (`Gadus morhua`, `Urophycis sp.`, `Gadidae`) |
| 5 | `UniqID` or `UniqueID` | Voucher, tissue, or specimen identifier. A web address works too; the GUI shows it as a link |

- Header names are matched ignoring case and surrounding spaces (`id, r1 ,R2`
  is fine). The order is not flexible: the right names in another order make the
  mapfile "broken" and no samples are loaded.
- Any columns after the fifth are kept, by header name, in the sample's
  `extra_json` column (JSON). Use them for collector, station, plate, and
  similar. The GUI does not show them yet; query them with SQL (see
  [data model](data-model.md#example-queries)).

## Row rules

| Situation | Result |
|---|---|
| Blank line | Ignored |
| Empty `ID` | Row skipped, with a warning |
| `ID` already used earlier in this mapfile | Later row skipped, with a warning; the first is kept |
| Empty `Taxon` | Loaded as `NA`, flag `na_taxon` |
| Empty `UniqID` | Loaded as `NA`, flag `na_uniqid` |
| Empty `R1` or `R2` | Sample loaded with no file for that read, flag `missing_r1` / `missing_r2` |
| `R1` and `R2` name the same file | File cataloged once, as R1, flag `r1_eq_r2` |
| Same file name on two rows | Warning; the last row's sample and direction win |
| More cells than header columns (often an unquoted comma) | Loaded, flag `extra_cells`, with a warning |
| `UniqID` already used in another project | Loaded, with a warning ("possible re-sequence"); `validate` also lists shared UniqIDs |
| File name contains a control character | That file is not cataloged, with a warning |

If every row is skipped, the project gets status "no usable rows": its files are
cataloged from disk with no samples, and `ingest` counts it as rejected.

## How R1 and R2 are matched to files

- `R1` and `R2` are usually bare file names (`S01_R1.fastq.gz`), matched by
  exact name against every FASTQ found anywhere under the project folder (all
  subfolders). Which files count as FASTQs is set by `fastq_extensions`
  (default `fastq.gz,fq.gz`, see [config](config.md)).
- If the same file name exists in two subfolders (say `Run1/` and `Run2/`), a
  bare name picks the one that sorts last by path, with a warning. To name a
  specific one, write the path under the project folder with forward slashes:
  `Run1/S01_R1.fastq.gz`.
- A listed file that is not on disk is still cataloged (with no size) and reported
  as "in mapfile, not on disk".
- A FASTQ on disk that the mapfile does not list is not cataloged; it is reported
  as "on disk, not in mapfile".

## Encodings and CSV format

- Standard CSV: comma-separated, double quotes around any value that contains a
  comma.
- Accepted encodings: UTF-8 (with or without a byte-order mark), UTF-16 with a
  byte-order mark, and Windows cp1252 (what Excel's plain "CSV" saves on Windows).

## What ingest records

Each project gets a mapfile status, shown in the GUI `mapfile` column:

| Status | GUI label | When |
|---|---|---|
| `ok` | OK | Folder and mapfile present, every row clean |
| `flagged` | flagged rows | Some rows were repaired or skipped (see the flags) |
| `missing_mapfile` | no mapfile | Folder with no mapfile |
| `missing_seqdata` | no data folder | Mapfile with no folder |
| `broken_mapfile` | broken mapfile | Header does not start with the five required columns |
| `invalid_mapfile` | no usable rows | Header fine, no row could be loaded |

The project row also stores a plain-English explanation, shown when you click the
project in the GUI. See [statuses](statuses.md) for every status field.

## Flag tokens

Stored in `samples.flags`, `;`-separated, shown in the GUI Samples `flags`
column. Empty when the row was clean.

| Token | Meaning |
|---|---|
| `na_taxon` | Taxon was empty, set to `NA` |
| `na_uniqid` | UniqID was empty, set to `NA` |
| `missing_r1` | R1 file name was empty, no R1 file cataloged |
| `missing_r2` | R2 file name was empty, no R2 file cataloged |
| `r1_eq_r2` | R1 and R2 name the same file, only R1 cataloged |
| `extra_cells` | The row had more cells than the header |

Fix the mapfile and run `ingest` again to clear them.

## Project folder names

Every top-level folder in the sequence-data root is a project; its name is the
`project_id`. Two naming patterns fill in extra columns:

| Pattern | Example | `source` | `project_number` | `description` |
|---|---|---|---|---|
| `genohub-<digits>_<text>` | `genohub-2054899_OKEX01` | `genohub` | `2054899` | `OKEX01` |
| `LAB-<run>_<text>` (run has no underscore) | `LAB-RUN07_GulfSurvey` | `LAB` | `RUN07` | `GulfSurvey` |

Any other name works; those three columns stay empty.

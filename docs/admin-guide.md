# seqledger admin guide

How to create, fill, check, and maintain a seqledger catalog. For data managers.
Researchers who only browse want the [user guide](user-guide.md).

Contents

1. [How it works](#how-it-works)
2. [Directory layout](#directory-layout)
3. [Install, update, back up, restore](#install-update-back-up-restore)
4. [First catalog](#first-catalog)
5. [Mapfiles](#mapfiles)
6. [Ingest](#ingest)
7. [Validate](#validate)
8. [Checksums](#checksums)
9. [Integrity](#integrity)
10. [Taxonomy](#taxonomy)
11. [Hosting the GUI](#hosting-the-gui)
12. [Routine runbook](#routine-runbook)
13. [Logs](#logs)
14. [The one-writer rule](#the-one-writer-rule)
15. [Permissions and privacy](#permissions-and-privacy)
16. [Recovery recipes](#recovery-recipes)

Command details are in the [CLI reference](reference/cli.md); terms are in the
[glossary](glossary.md).

## How it works

The catalog is one SQLite file. It holds only metadata and checksums (a few MB),
never the FASTQs, and lives on Store next to the data it describes. Every
top-level folder under the sequence-data root is a project; each project has a
mapfile (`<project>_mapfile.csv`) in the metadata folder that lists its samples
and their R1/R2 files. `ingest` pairs folders with mapfiles and loads both. Three
separate checks then fill in status columns: `validate` compares the mapfile with
what is on disk, `checksums` compares md5 listings of Store and the P-drive
backup, and `integrity` reads every FASTQ to the end to catch truncation and
corruption. `taxonomy` resolves the free-text Taxon of each sample against NCBI
(offline, from a downloaded taxdump) and WoRMS (online). Store is not mounted on
ordinary compute nodes, so jobs that read Store (`integrity --batch`,
`gui --qsub`, Grab & Go copy jobs) run on the I/O queue (lTIO). Batch jobs never
write the catalog; they write result files that you merge from a login node.

The core commands use only the Python standard library. The GUI and the tests
need streamlit, pandas, and plotly.

## Directory layout

Example layout on Hydra. Your paths may differ; the catalog records them as
`seqdata_root` and `metadata_root` ([config](reference/config.md)).

```
/store/nmnh_ocean_dna/public/
    raw_sequence_data/                        # seqdata_root
        genohub-2054899_OKEX01/               # one project folder
            22030-14-01/                      # any depth of subfolders
                S01_L001_R1_001.fastq.gz
                S01_L001_R2_001.fastq.gz
        LAB-RUN07_GulfSurvey/
            S01_R1.fastq.gz
            S01_R2.fastq.gz
    raw_sequence_metadata/                    # metadata_root (flat, no subfolders)
        genohub-2054899_OKEX01_mapfile.csv
        LAB-RUN07_GulfSurvey_mapfile.csv
    oceandna_catalog.db                       # the catalog (--db, SEQLEDGER_DB)
    .taxonomy/                                # next to the catalog
        names.dmp  nodes.dmp                  # NCBI taxdump (downloaded once)
        taxdump.sqlite                        # index built from it
        TAXDUMP_VERSION.txt                   # download date
        worms_cache.sqlite                    # cached WoRMS answers
    taxonomy_review.csv                       # NCBI review CSV, next to the catalog
    worms_review.csv                          # WoRMS review CSV

<directory you run integrity --batch from>/
    integrity_batch/
        scripts/integrity_<project>.job       # one qsub script per project
        logs/integrity_<project>.log          # job logs
        results/<project>.json                # per-file results
        results/<project>.json.done           # written when the job finished

~/.seqledger_gui/                             # gui --qsub job, log, ready files
```

Project folder names that follow `genohub-<number>_<description>` or
`LAB-<run>_<description>` fill the `source`, `project_number`, and `description`
columns. Other names work, with those columns left empty. See
[mapfile reference](reference/mapfile.md#project-folder-names).

## Install, update, back up, restore

### Install

You need conda on Hydra first; see the
[Hydra conda page](https://confluence.si.edu/spaces/HPC/pages/163152340/Conda+Anaconda+Miniconda).

```bash
ssh jsmith@hydra-login01.si.edu
git clone https://github.com/dmacguigan/seqledger.git
cd seqledger
conda env create -f environment.yml     # creates an env named seqledger
conda activate seqledger
pip install -e '.[gui]'
seqledger --version    # prints the installed version
```

The `seqledger` command is available whenever the `seqledger` env is active;
run `conda activate seqledger` in each new login.

Requirements: Python 3.10 or newer; `rclone` for making md5 listings (on Hydra:
`module load tools/rclone/1.66.0`); SGE `qsub` for batch jobs. Without conda,
`pip install -e '.[gui]'` (or `.[test]` for the test suite) in any Python 3.10+
environment works. The core commands also run from the repository root without
installing: `python -m seqledger -h`.

Optional: if `python-isal` is installed (`pip install isal`), `integrity` uses
it to decompress about twice as fast. This only helps when the check is
CPU-bound, not on a slow network mount.

Batch jobs and the GUI job run `source ~/.bashrc` and then `conda activate
<conda_env>` on the compute node. Keep the environment name `seqledger`, or set
the catalog's `conda_env` to match ([config](reference/config.md)), and make sure
`conda activate` works in a fresh login shell (`conda init bash`).

### Update

1. Read the new entries in [CHANGELOG.md](../CHANGELOG.md).
2. [Back up the catalog](#back-up).
3. `cd seqledger && git pull`, then `pip install -e '.[gui]' --no-deps` so
   `seqledger --version` reports the new release (the editable install picks up
   code changes by itself, but not the version). If dependencies changed, run
   `conda env update -f environment.yml` instead.
4. Run `seqledger init-db`. It creates any new tables and is safe to re-run. It
   does not migrate old tables: if it stops with `catalog table '<name>' is from
   an older seqledger`, run `init-db` once with the 0.1.0 code, which still
   carries the migrations, then again with the current code:

   ```bash
   cd seqledger
   git checkout f3174b6          # the 0.1.0 release
   seqledger init-db
   git checkout main
   seqledger init-db
   ```

### Back up

Make a consistent copy with SQLite's backup command. It is safe while researchers
are reading:

```bash
sqlite3 /store/nmnh_ocean_dna/public/oceandna_catalog.db \
    ".backup /store/nmnh_ocean_dna/public/catalog_backups/oceandna_catalog_2026-10-19.db"
```

If `sqlite3` is not installed, the same from Python:

```bash
python -c "import sqlite3; s=sqlite3.connect('/store/nmnh_ocean_dna/public/oceandna_catalog.db'); d=sqlite3.connect('/store/nmnh_ocean_dna/public/catalog_backups/oceandna_catalog_2026-10-19.db'); s.backup(d); d.close()"
```

A plain `cp` is safe only when no seqledger command is writing. Back up before
`init-db`, before any `--prune`, and before a new release.

### Restore

1. Make sure no seqledger command is writing (see [one writer](#the-one-writer-rule)).
   Ask researchers to close their sessions.
2. Keep the damaged file for inspection: `mv oceandna_catalog.db oceandna_catalog.db.broken`.
3. Copy the backup into place and fix its permissions:

   ```bash
   cp /store/nmnh_ocean_dna/public/catalog_backups/oceandna_catalog_2026-10-19.db \
       /store/nmnh_ocean_dna/public/oceandna_catalog.db
   chmod 640 /store/nmnh_ocean_dna/public/oceandna_catalog.db
   ```

4. Re-run `ingest` to pick up anything that changed since the backup. Rerun
   `checksums` and `integrity --collect` if their inputs are still around.

## First catalog

```bash
seqledger --db /store/nmnh_ocean_dna/public/oceandna_catalog.db init-db \
    --name "Ocean DNA sequence data catalog" \
    --seqdata-root /store/nmnh_ocean_dna/public/raw_sequence_data \
    --metadata-root /store/nmnh_ocean_dna/public/raw_sequence_metadata \
    --set catalog_slug=oceandna
export SEQLEDGER_DB=/store/nmnh_ocean_dna/public/oceandna_catalog.db
seqledger init-db --show
```

- `init-db` creates the file (mode 600, owner only; see
  [permissions](#permissions-and-privacy)) and the tables.
- `--name`, `--seqdata-root`, `--metadata-root` set the matching config keys.
  Every other key goes through `--set KEY=VALUE` (repeatable). `init-db -h`
  lists the keys and their defaults; the [config reference](reference/config.md)
  explains them.
- Re-run `init-db` any time to change config. Keys you do not pass keep their
  value.
- `init-db --show` prints the config and changes nothing.
- `--set` accepts any key name without checking it. Check spelling with `--show`.

With the roots stored, `ingest`, `validate`, and `integrity` need no root flags.

## Mapfiles

Each project needs `<project>_mapfile.csv` in the metadata folder, where
`<project>` is exactly the project folder name. The first five columns must be
`ID,R1,R2,Taxon,UniqID` in that order; R1 and R2 are file names (not paths) found
anywhere under the project folder. Full rules, an example, and the encodings
accepted are in the [mapfile reference](reference/mapfile.md).

## Ingest

```bash
seqledger ingest
```

What it does, in order:

1. Lists every top-level folder in `seqdata_root` and every `*_mapfile.csv` in
   `metadata_root`, and pairs them by name. Both sides are included, so a folder
   with no mapfile and a mapfile with no folder both show up.
2. Loads each pair. FASTQs are found recursively under the project folder.
   Problems never block the rest: each project gets a mapfile status (OK,
   flagged rows, no mapfile, no data folder, broken mapfile, no usable rows; see
   [statuses](reference/statuses.md#projects-mapfile-status)). A project with no
   usable mapfile still has its files cataloged from disk.
3. Resolves new Taxon names against NCBI (downloads the taxdump on first use,
   about 72 MB). Skip with `--skip-taxonomy`. If the download fails (offline
   node), ingest still succeeds and tells you to run `taxonomy resolve` later.
   WoRMS is never run by ingest.
4. Refreshes the data-files check (same as `validate`) for what it loaded.

Re-running is cheap and safe: rows are updated in place, and unchanged projects
cost little. It does not run `integrity` (slow) or `checksums`.

Root flags: `--seqdata-root` and `--metadata-root` override the configured roots
for one run. Ingest needs both, from flags or config, and stops if either folder
does not exist (for example Store not mounted).

### Removed samples and projects

Ingest never deletes anything unless you ask. A plain run reports:

- samples that are in the catalog but no longer in their mapfile ("N sample(s) in
  catalog, not in mapfile (kept; --prune removes)"), and
- projects whose folder and mapfile are both gone ("N project(s) gone from disk
  and mapfiles", followed by up to 10 names and "kept; --prune-projects removes
  them").

To delete them:

- `--prune` deletes samples dropped from their mapfile, and file rows that the
  mapfile no longer points to. Per project, it refuses (with a warning, the
  project is left alone) to drop more than half of a project's samples when the
  project has more than 2; that usually means a truncated mapfile. It also keeps
  all file rows when a mapfile references no files at all.
- `--prune-projects` deletes projects gone from both roots, with all their
  samples and files. Only projects ingested from this same `seqdata_root` are
  considered. It refuses, and ingest exits 2, when:
  - either root folder does not exist,
  - discovery finds no projects at all, or
  - it would delete more than half of the catalog's projects under this root.

  There is no flag to override these refusals. If you really removed that many
  projects, delete them by hand (see [recovery](#delete-projects-by-hand)).

Both prune flags ask for confirmation (`[y/N]`) in a terminal. Without a terminal
(cron, a pipe, a batch job) they are refused with exit code 2 unless you add
`--yes`. Back up first.

```bash
seqledger ingest --prune --prune-projects            # asks first
seqledger ingest --prune --prune-projects --yes      # for scripts
```

The menu (`seqledger`, option 4) runs a plain ingest, then offers to delete what
it listed if you type `DELETE`.

Exit codes: 0 ok (warnings and flagged rows included), 1 if any project was
rejected (broken mapfile or no usable rows), 2 if a prune was refused.

## Validate

```bash
seqledger validate
```

Re-checks every project without loading anything:

- **Data files**: compares the mapfile's file list with the FASTQs on disk, in
  both directions, by relative path. The result is stored on the project and
  shown in the GUI `data_files` column. If a project's data folder is gone, the
  project is marked unchecked with "data folder not found". If the whole root
  is unreachable, the stored result is left as it was.
- **Checksum**: summarizes the stored md5 comparison (from `checksums`).
- **Shared UniqIDs**: notes a UniqID used in more than one project.

Which root it scans: `--seqdata-root` if given, else the configured
`seqdata_root`, else the root each project was ingested from. A configured root
therefore overrides every project's own root; if your projects sit under two
different roots, unset `seqdata_root` (`init-db --set seqdata_root=`) and let each
project use its own.

Exit codes: 0 ok, 1 if any project has missing or extra files or an md5 mismatch.

## Checksums

`checksums` checks that the P-drive backup is an exact copy of Store. You make
two md5 listings with `rclone md5sum` (one per side), then load and compare them.

### Make the listings

Both listings must be rooted at the `raw_sequence_data` level, so that every
path starts with the project folder:

```
b8e43f335c0fb6954f32e9d0da7c12ad  genohub-2054899_OKEX01/22030-14-01/S01_L001_R1_001.fastq.gz
```

Example from a Mac that mounts the P-drive and has an rclone remote named
`SI-Hydra` for Hydra (set it up once with `rclone config`, type sftp):

```bash
rclone md5sum SI-Hydra:/store/nmnh_ocean_dna/public/raw_sequence_data > store.md5
rclone md5sum /Volumes/nmnh-ocean-dna/Hydra_backup/store/raw_sequence_data > pdrive.md5
```

To list one project only, keep the same root and add a filter, so paths still
start with the project folder:

```bash
rclone md5sum /Volumes/nmnh-ocean-dna/Hydra_backup/store/raw_sequence_data \
    --include "genohub-2054899_OKEX01/**" > pdrive_OKEX01.md5
```

Plain `md5sum` output also works (run it from inside the root so paths are
relative). Upper-case hashes, a leading `./`, and Windows backslashes are fine.
Reading every byte is the slow part; on Hydra, run it on the I/O queue or after
hours.

### Load and compare

```bash
seqledger checksums --store store.md5 --pdrive pdrive.md5
```

- Only projects that appear in the listings are updated. Other projects keep
  their result. `--full` also resets projects absent from the listings (their
  files become "not compared").
- In a listed project, a cataloged file missing from both listings is reset to
  "not compared".
- Listing entries that are not in the catalog are reported as "on disk, not in
  mapfile".
- Safety check: if either listing has fewer than half as many entries as there
  are cataloged files in scope, nothing is changed and the command stops with an
  error. Fix the listing, or pass `--force` if the small listing is intended.
- `--project PROJECT` limits loading to one project.
- `--source ingest|backfill` is only a label stored with each md5 (default
  `backfill`). It changes nothing else.
- A project's backup counts as verified when every one of its files has the same
  md5 on both sides.

Exit codes: 0 ok, 1 if any file's md5 differs, 1 with an `error:` line if a
listing looks truncated.

## Integrity

`integrity` decompresses every FASTQ to the end (the same test as `gzip -t`: it
catches truncation, CRC errors, and bit-rot), checks that the line count is a
multiple of 4, counts reads, and compares R1 and R2 read counts per sample. It
does not check each record's `@` and `+` lines.

Results per file: `ok`, `gzip_error`, `format_error` (shown as "bad FASTQ line
count"), or `unchecked` (not found or unreadable). A file that passed before and
has the same size is skipped, so re-runs only read new or changed files. Use
`--force` to re-read everything (the only way to catch bit-rot that keeps the
size).

### Local run

```bash
seqledger integrity
seqledger integrity --project genohub-2054899_OKEX01 --jobs 16
```

Reads files from the current machine. On Hydra, Store is only readable from
login nodes and the I/O queue, so a big local run loads a shared login node;
prefer `--batch`. `--jobs` sets parallel reads (default: up to 8; on a network
mount 16 to 32 can be faster). A progress counter shows on the terminal.

Results are saved at least every 200 files or 30 seconds. Press Ctrl-C to stop:
it prints `interrupted: N file(s) checked, results saved`. Run it again to carry
on where it stopped.

Root: `--seqdata-root` if given, else the configured `seqdata_root`, else each
project's own root (same rule as [validate](#validate)).

### Batch run on the I/O queue

```bash
cd /home/jsmith/seqledger_work
seqledger integrity --batch --only-unchecked
```

- Writes one script per project to `./integrity_batch/scripts/` and submits each
  to the configured `io_queue` (default `lTIO.sq`). `--project` submits one.
- Each job opens the catalog read-only and writes
  `integrity_batch/results/<project>.json`, plus `<project>.json.done` when it
  finishes. Jobs never write the catalog.
- `--only-unchecked` skips projects whose files have all been checked before.
- `--slots` (default 4) sets CPU slots and parallel reads per job; `--mem`
  (default 2) sets GB per slot. lTIO limits: 6 slots and 2 running jobs per user,
  8 GB per slot, 72 hours. With more than 2 projects, the rest wait in the queue.
- `--no-submit` writes the scripts without submitting them. `--batch-dir`
  changes `./integrity_batch`.

When the jobs are done, merge from a login node:

```bash
seqledger integrity --collect integrity_batch/results
```

This is the step that writes the catalog. It skips any result without a `.done`
marker (job still running or killed) and prints which ones.

**Resume.** Each job saves its JSON every 200 files. If a job is killed at the
72-hour or CPU limit, submit the same script again
(`qsub integrity_batch/scripts/integrity_<project>.job`); it reloads its JSON and
skips files already checked. Then `--collect` again.

Run batch, resubmit, and collect from the same directory, so `integrity_batch/`
is the same folder each time. The menu (option 5) looks for
`integrity_batch/results` in the current directory.

Exit codes: 0 ok (files "not checked" do not count as failures), 1 if any file
has a gzip or format error, 2 if `qsub` is missing or a submission failed (the
scripts are still written; submit them by hand on a login node).

## Taxonomy

The Taxon column of a mapfile is free text. `taxonomy resolve` matches each
distinct Taxon to NCBI (TaxID and lineage) or WoRMS (AphiaID and lineage) and
writes a review CSV; you check the doubtful rows and `taxonomy apply` them.

### NCBI

```bash
seqledger taxonomy resolve
```

- Uses a local copy of the NCBI taxdump in `<catalog folder>/.taxonomy/`
  (downloaded once, about 72 MB, then indexed; the folder grows to over 1 GB).
  The download date is in `.taxonomy/TAXDUMP_VERSION.txt`. Refresh it with
  `--force-download`. Compute nodes may have no internet: run this on a login
  node.
- Matching: exact name first, then fuzzy (genus-anchored). Match types: `exact`,
  `fuzzy_species`, `fuzzy_genus`, `fuzzy_higher`, `ambiguous` (a name shared by
  two kingdoms), `unresolved`.
- By default only taxa never resolved before are looked up.
  `--refresh-unconfirmed` also redoes earlier non-confirmed results. `--redo`
  redoes everything, but never overwrites a confirmed taxon (its new machine
  result appears only in the review CSV).

### WoRMS

```bash
seqledger taxonomy resolve --source worms
seqledger taxonomy resolve --source both
```

Queries the WoRMS web service (needs internet every time there are new names;
answers are cached in `.taxonomy/worms_cache.sqlite`). Synonyms are followed to
the accepted name. Only `exact` and `near_1` (one-letter difference) matches get
an AphiaID automatically. Looser matches are stored as `review_phonetic`,
`review_near_2`, `review_near_3`, or `review_match_quarantine`, with no AphiaID,
for you to decide. If WoRMS cannot be reached it prints a warning and the NCBI
side (with `both`) still runs.

### The review CSV

- Written next to the catalog: `taxonomy_review.csv` (NCBI) or `worms_review.csv`
  (WoRMS). If that file already exists, the new one is
  `taxonomy_review_2026-10-19.csv` (then `_2`, `_3`), so unapplied edits are never
  overwritten. The command prints the path when rows need a check. Ingest writes
  the NCBI one the same way when it resolves new names.
- One row per taxon looked up, with the machine result, the candidates in
  `alternatives` / `worms_alternatives`, and a last column `confirmed_taxid` or
  `confirmed_aphia_id`.
- That last column is pre-filled **only for exact matches**. A filled cell means
  "I confirm this ID".
- To review: open the CSV (Excel is fine). For each fuzzy, ambiguous, review, or
  unresolved row you have checked, type the correct ID into the confirmed
  column. Leave a cell empty to leave that taxon unconfirmed. Clear a pre-filled
  exact cell if you disagree with it.
- Apply it, using the path that was printed:

  ```bash
  seqledger taxonomy apply --review /store/nmnh_ocean_dna/public/taxonomy_review.csv
  seqledger taxonomy apply --source worms --review /store/nmnh_ocean_dna/public/worms_review.csv
  ```

- Every row with a non-empty confirmed cell is applied, including the pre-filled
  exact rows. Each applied taxon gets match type `confirmed`, and later resolves
  never change it. An empty cell does not undo an earlier confirmation; to change
  a confirmed taxon, apply a row with the right ID.
- An ID that is not a number, or not found (in the local taxdump for NCBI, in
  WoRMS for WoRMS), is skipped and listed as `SKIP`; the other rows still apply.
- CSVs saved by Excel (UTF-8 with or without BOM, Windows cp1252, UTF-16) are
  read correctly. `--source` must match the CSV: an NCBI CSV with
  `--source worms` stops with an error.

`seqledger query taxa` lists taxa without an exact or confirmed NCBI match.

## Hosting the GUI

Two ways to give users the GUI. Users start it themselves; see the
[user guide](user-guide.md).

**On the I/O queue (recommended).** `seqledger gui --qsub` submits a job to
`io_queue` that reads the master catalog on Store directly. Nothing to sync. Each
session uses one of the user's 2 lTIO job slots and ends after 72 hours.
`--mem` (default 6 GB) may need raising for a large catalog. The job, log, and
ready files go to `~/.seqledger_gui/` and are deleted after 7 days.

**From a copy on Scratch.** Compute nodes mount Scratch, so a copy there can be
served from any interactive node without using lTIO slots. Refresh the copy with
SQLite's backup command (safe while you write the master), for example nightly
from cron:

```bash
sqlite3 /store/nmnh_ocean_dna/public/oceandna_catalog.db \
    ".backup /scratch/nmnh_ocean_dna/oceandna_catalog.db"
chmod 644 /scratch/nmnh_ocean_dna/oceandna_catalog.db
```

Users then start an interactive session (for example `qrsh`) and run
`seqledger --db /scratch/nmnh_ocean_dna/oceandna_catalog.db gui --port 8501`; it
prints the tunnel command. Scratch is not backed up: the copy is disposable,
never write to it. The GUI's banner shows the file's last-updated time, so users
can see when a copy is stale. If the copy's tables are older than the GUI code,
the GUI says so and asks for a re-sync.

`init-db` also records the path of the dismissals log (see
[Permissions and privacy](#permissions-and-privacy)) in the config key
`dismiss_log`, so a Scratch copy uses the master's log. Re-run `init-db` on the
master once so the key is set. If the log's folder does not exist or the log
cannot be read, the GUI sidebar shows "Dismissals unavailable" and every Dismiss
and Undo button is hidden.

The config travels inside the `.db` file, so a copy has the same settings.

## Routine runbook

A weekly round, after new data and new mapfiles land:

1. Back up: `sqlite3 "$SEQLEDGER_DB" ".backup /store/nmnh_ocean_dna/public/catalog_backups/oceandna_catalog_2026-10-19.db"`.
2. `seqledger ingest -v`. Fix any mapfile problems it lists and run it again.
3. If it printed a review CSV, review it and `seqledger taxonomy apply --review <path>`.
4. `seqledger integrity --batch --only-unchecked`. When the jobs finish:
   `seqledger integrity --collect integrity_batch/results`.
5. After the P-drive backup has run: make both md5 listings, then
   `seqledger checksums --store store.md5 --pdrive pdrive.md5`.
6. `seqledger validate`, then `seqledger query unbacked` for projects still not
   backed up.
7. If you serve a Scratch copy, refresh it.

### Cron example

Cron runs with a minimal environment, so call the environment's `seqledger`
directly and set `SEQLEDGER_DB` in the crontab. `-vv` puts per-file detail into
the log. Find the binary with `which seqledger` while the environment is active.

```
SEQLEDGER_DB=/store/nmnh_ocean_dna/public/oceandna_catalog.db
# Monday 02:00: back up, then ingest (no pruning) and validate
0 2 * * 1  sqlite3 /store/nmnh_ocean_dna/public/oceandna_catalog.db ".backup /store/nmnh_ocean_dna/public/catalog_backups/weekly.db"
15 2 * * 1 /home/jsmith/miniconda3/envs/seqledger/bin/seqledger ingest -vv >> /home/jsmith/seqledger_logs/ingest.log 2>&1
45 2 * * 1 /home/jsmith/miniconda3/envs/seqledger/bin/seqledger validate -vv >> /home/jsmith/seqledger_logs/validate.log 2>&1
```

Notes:

- Exit codes let cron tell you about problems: 0 ok, 1 problems found or an
  error, 2 refused ([CLI reference](reference/cli.md#exit-codes)).
- Progress counters only print on a terminal, so logs stay clean.
- A prune in cron needs `--yes`; without it ingest refuses and exits 2. The
  half-catalog refusal still applies with `--yes`.
- `integrity --batch` from cron also needs `qsub` on the `PATH`, which a cron
  environment may not have. If it reports `qsub not found`, run it by hand.

## Logs

| What | Where |
|---|---|
| CLI summaries | stdout. Progress counters and `(using configured seqdata-root: <path>)` notes go to stderr. Redirect both: `>> run.log 2>&1` |
| Integrity batch jobs | `<batch-dir>/logs/integrity_<project>.log` |
| Integrity batch results | `<batch-dir>/results/<project>.json` and `.json.done` |
| Integrity run history | `validation_log` table (one row per project per run, written by local runs and `--collect`) |
| GUI job | `~/.seqledger_gui/gui_<pid>_<time>.log` (with `.job` and `.ready`), deleted after 7 days |
| Grab & Go copy job | `seqledger_rclone_copy_<stamp>.log` in the directory `qsub` was run from |
| Taxdump download date | `.taxonomy/TAXDUMP_VERSION.txt` next to the catalog |
| Full error traceback | add `--debug` before the subcommand: `seqledger --debug ingest` |

## The one-writer rule

Run only one writing command at a time on a catalog. Writers: `init-db` (not
`--show`), `ingest`, `validate`, `checksums`, `integrity` (local and
`--collect`), `taxonomy resolve`, `taxonomy apply`. Readers that are always safe
alongside a writer: `query`, `init-db --show`, the GUI, and batch integrity jobs.

A second writer waits up to 60 seconds for the lock, then fails with
`error: database is locked`. Researchers see "Catalog is being updated, retry
shortly." during long writes. Do not run two writers from different machines
against the same file on a network filesystem.

## Permissions and privacy

- `init-db` creates a new catalog with mode 600 (only you can read it). Before
  researchers can open it, grant read access, for example
  `chmod 640 oceandna_catalog.db` with a shared group, or `chmod 644`.
- Writers need write access to the catalog file and its folder (SQLite writes a
  journal file next to it, and the review CSVs and `.taxonomy/` land there).
- Researchers need read access to the catalog file only.
- Config values that go into job scripts (`conda_env`, `io_queue`,
  `rclone_module`, `login_host`, `catalog_slug`) may only contain letters, digits,
  and `. _ @ : / -`.
- `owner_name` (on projects and files) is the Unix user who owns the data on disk.
  It is personal information. It appears in the GUI and its CSV exports; do not
  publish those outside the lab.
- GUI dismissals are kept in a hidden file next to the catalog, named
  `.<catalog name>_dismissed.tsv` (for `oceandna_catalog.db` it is
  `.oceandna_catalog_dismissed.tsv`). The first dismiss creates it with mode
  664. To set it up ahead of time, run `touch <path> && chmod 664 <path>` (or 666
  without a shared group). If the file is not writable, the GUI shows an error
  with the `chmod` command. It is plain tab-separated text, one line per dismiss
  or undo, with the columns time, user, action, scope, project, item, kind, and
  note; the last line for an item wins. It is safe to edit or truncate.
  It holds user names and notes, so treat it like the catalog.

## Recovery recipes

**A bad ingest or prune.** Restore the last backup
([restore](#restore)), fix the mapfile or mount, ingest again.

**`init-db` says a table is from an older seqledger.** The catalog was made by
an older release. Run `init-db` once with the 0.1.0 code and once with the
current code ([Update](#update) step 4).

**First ingest on an offline node.** Ingest succeeds and skips taxonomy. Later,
on a login node with internet: `seqledger taxonomy resolve`. To prepare the
taxdump elsewhere, copy a finished `.taxonomy/` folder next to the catalog or pass
`--taxdir`.

**`qsub not found`.** You are not on a login node. The integrity scripts were
written anyway; submit them there with `qsub integrity_batch/scripts/integrity_<project>.job`.

**Batch job ends at once, log says `conda: command not found` or activate
fails.** Conda is not set up for non-interactive shells: run `conda init bash`,
and check that `conda activate seqledger` works in a fresh login.

**Jobs stuck in `qw`.** lTIO runs 2 jobs per user at a time. GUI sessions,
integrity jobs, and copy jobs share that limit. `qstat` and `qdel` what you do not
need.

**`--collect` skips a file: `no .done marker`.** That job is still running or
was killed. Wait, or resubmit its script, then collect again.

**`checksums` stops with `listing is empty or much smaller than the catalog`.**
The listing is truncated or was made at the wrong root (paths must start with
the project folder). Nothing was changed. Remake it; use `--force` only for a
deliberately partial listing.

**`error: database is locked`.** Another writer is running. Wait for it and
retry.

<a id="delete-projects-by-hand"></a>
**Delete projects by hand** (when `--prune-projects` refuses but you are sure).
Back up first, then:

```bash
sqlite3 "$SEQLEDGER_DB" "PRAGMA foreign_keys=ON; DELETE FROM projects WHERE project_id='genohub-2054899_OKEX01';"
```

`PRAGMA foreign_keys=ON` makes the delete remove the project's samples, files,
and check results too.

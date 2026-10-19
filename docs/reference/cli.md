# CLI reference

Every `seqledger` subcommand and flag, with the real `--help` text and notes on
behaviour. For how to use them together, see the [admin guide](../admin-guide.md).

Contents

- [Global flags](#global-flags)
- [Output](#output)
- [Exit codes](#exit-codes)
- [The menu](#the-menu)
- [init-db](#init-db)
- [ingest](#ingest)
- [checksums](#checksums)
- [validate](#validate)
- [integrity](#integrity)
- [taxonomy resolve](#taxonomy-resolve)
- [taxonomy apply](#taxonomy-apply)
- [query](#query)
- [gui](#gui)

## Global flags

```
usage: seqledger [-h] [--db DB] [--debug] [-v] [--version]
                 {init-db,ingest,checksums,validate,integrity,query,taxonomy,gui}
                 ...

seqledger: sequence-data catalog CLI

positional arguments:
  {init-db,ingest,checksums,validate,integrity,query,taxonomy,gui}
    init-db             create the catalog schema; optionally set per-catalog
                        config
    ingest              auto-discover projects from a seqdata + metadata dir;
                        auto-run taxonomy, refresh data-files check (integrity
                        is a separate step)
    checksums           load + compare rclone md5sum output
    validate            re-check the catalog (data-files + checksum)
    integrity           gzip + FASTQ integrity check of cataloged files
    query               read-only lookups (see seqledger query -h)
    taxonomy            resolve sample taxa to NCBI TaxIDs + WoRMS AphiaIDs
    gui                 launch Streamlit browse GUI (locally, or on lTIO via
                        --qsub)

options:
  -h, --help            show this help message and exit
  --db DB               path to catalog SQLite DB (default: $SEQLEDGER_DB,
                        else catalog.db)
  --debug               show the full traceback on error instead of a one-line
                        message
  -v, --verbose         more detail: -v per project, -vv per file
  --version             show program's version number and exit
```

- `--db PATH`: the catalog file. Default: `$SEQLEDGER_DB` if set, else
  `catalog.db` in the current directory. Accepted before or after the subcommand
  (`seqledger --db /path/to/catalog.db query summary` and
  `seqledger query summary --db /path/to/catalog.db` are the same).
- `-v`, `-vv`: more detail, before or after the subcommand.
- `--debug`: print the full Python traceback instead of a one-line `error:`.
  Before or after the subcommand.
- Every command except `init-db` stops with `no catalog at '<path>'` (exit 1) if
  the catalog file does not exist, instead of creating an empty one.
- Run without installing: `python -m seqledger` from the repository root.

## Output

- `ingest`, `checksums`, `validate`, and `integrity` print a short summary by
  default: counts, a "Problems:" list capped at 10 projects, and at most one
  "Next:" hint.
- `-v` lists every project (problems or "OK"); `-vv` adds every per-file line,
  sorted by path.
- Progress counters and notes such as `(using configured seqdata-root: <path>)` go to
  stderr. Progress counters only appear when stderr is a terminal, so cron and
  job logs stay clean.
- `query` prints a tab-separated table on stdout and nothing else. When nothing
  matches it prints `(none)` on stderr.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Ran cleanly. Warnings (flagged rows, files not yet checked) do not change this |
| 1 | Problems found: `ingest` rejected a project; `validate` found missing or extra files or an md5 mismatch; `integrity` found a gzip or format error; `checksums` found an md5 mismatch. Also any error that stops a command (`no catalog at`, `error: <message>`, a bad `--set` key or value, a missing root folder) |
| 2 | Refused or usage error: a prune refused (no `--yes` without a terminal, an answer other than yes, or a safety refusal of `--prune-projects`); `integrity --batch` could not run `qsub`; bad command-line arguments; `seqledger` with no subcommand outside a terminal |
| 130 | Interrupted with Ctrl-C |

`query`, `taxonomy`, and `gui` exit 0 unless they hit an error (exit 1) or a usage
error (exit 2).

## The menu

`seqledger` with no subcommand, in a terminal, opens a text menu (see the
[README](../../README.md#the-seqledger-menu)). Each choice runs a normal command and
first prints it under "Same thing without the menu". `c` switches catalogs or
creates one with `init-db`.

## init-db

```
usage: seqledger init-db [-h] [--db DB] [--debug] [-v] [--name NAME]
                         [--seqdata-root SEQDATA_ROOT]
                         [--metadata-root METADATA_ROOT] [--set KEY=VALUE]
                         [--show]

Create or configure the catalog schema and set config. --show only prints the config (read-only).

options:
  -h, --help            show this help message and exit
  --db DB               path to catalog SQLite DB
  --debug               show the full traceback on error
  -v, --verbose         more detail: -v per project, -vv per file
  --name NAME           catalog display name (GUI title)
  --seqdata-root SEQDATA_ROOT
                        default root of raw_sequence_data for this catalog
  --metadata-root METADATA_ROOT
                        default dir of per-project '<project>_mapfile.csv'
                        files
  --set KEY=VALUE       set any config key (repeatable; keys listed below)
  --show                print the resolved config and exit (read-only; ignores
                        other flags)

other config keys (set with --set KEY=VALUE):
  catalog_slug (default: oceandna)
  conda_env (default: seqledger)
  rclone_module (default: tools/rclone/1.66.0)
  login_host (default: hydra-login01.si.edu)
  io_queue (default: lTIO.sq)
  backup_location (default: pdrive)
  fastq_extensions (default: fastq.gz,fq.gz)
```

- Creates the catalog file (mode 600) and any missing tables. Safe to re-run.
- Refuses a catalog whose tables come from an older seqledger: `catalog table
  '<name>' is from an older seqledger`. There are no automatic migrations.
  Back up before running it on an existing catalog.
- `--name`, `--seqdata-root`, `--metadata-root` set `catalog_name`,
  `seqdata_root`, `metadata_root`. Every other key: `--set KEY=VALUE`, repeatable.
  Unknown key names are stored without a warning; check with `--show`.
- `--show` opens the catalog read-only, prints every key with its value (stored
  or default), and changes nothing.
- See the [config reference](config.md).

```bash
seqledger --db /path/to/catalog.db init-db --set conda_env=myenv --set io_queue=sThM.q
seqledger --db /path/to/catalog.db init-db --show
```

## ingest

```
usage: seqledger ingest [-h] [--db DB] [--debug] [-v]
                        [--metadata-root METADATA_ROOT]
                        [--seqdata-root SEQDATA_ROOT] [--taxdir TAXDIR]
                        [--skip-taxonomy] [--prune] [--prune-projects] [--yes]

Ingest metadata, resolve new taxa, refresh the data-files check. Exits 1 if
any project FAILs, 2 if a prune is refused.

options:
  -h, --help            show this help message and exit
  --db DB               path to catalog SQLite DB
  --debug               show the full traceback on error
  -v, --verbose         more detail: -v per project, -vv per file
  --metadata-root METADATA_ROOT
                        dir holding the per-project '<project>_mapfile.csv'
                        files
  --seqdata-root SEQDATA_ROOT
                        root of raw_sequence_data; each top-level folder is a
                        project
  --taxdir TAXDIR       taxdump dir (default: <db dir>/.taxonomy)
  --skip-taxonomy       skip the taxonomy step
  --prune               delete catalog samples/files the CSV no longer lists
  --prune-projects      delete whole catalog projects that vanished from BOTH
                        the seqdata and metadata dirs (cascades to their
                        samples/files). Refuses if a root is missing, turns up
                        no projects, or more than half of the root's projects
                        would go, so a missing mount can't wipe the catalog.
  --yes                 skip the confirmation prompt for --prune / --prune-
                        projects
```

- Roots come from the flags, else the catalog config. Both are required. Stops if
  either folder does not exist.
- After loading: resolves new Taxon names against NCBI (unless
  `--skip-taxonomy`; `--taxdir` changes where the taxdump lives) and refreshes the
  data-files check. Despite the `--prune` help text, you do not need to run
  `validate` afterwards.
- A plain run lists samples dropped from mapfiles and projects gone from both
  roots, and keeps them.
- `--prune`: also refuses, per project, to drop more than half of a project's
  samples (when it has more than 2).
- `--prune-projects`: also refuses when either root folder is missing, or when it
  would delete more than half of the catalog's projects under this root. No flag
  overrides these.
- Both prune flags prompt in a terminal; without a terminal they are refused
  unless `--yes` is given.
- Details: [admin guide, ingest](../admin-guide.md#ingest).

## checksums

```
usage: seqledger checksums [-h] [--db DB] [--debug] [-v] --store STORE
                           --pdrive PDRIVE [--project PROJECT]
                           [--source {ingest,backfill}] [--full] [--force]

Load + compare rclone md5sum output from Store and P-drive. Exits 1 on any md5
mismatch.

options:
  -h, --help            show this help message and exit
  --db DB               path to catalog SQLite DB
  --debug               show the full traceback on error
  -v, --verbose         more detail: -v per project, -vv per file
  --store STORE         rclone md5sum output for Store
  --pdrive PDRIVE       rclone md5sum output for P-drive
  --project PROJECT     limit to one project_id
  --source {ingest,backfill}
                        label stored on the md5 rows (default backfill; ingest
                        only for listings made at ingest time)
  --full                also reset files in projects absent from the listings
  --force               load even if a listing looks truncated
```

- `--store`, `--pdrive`: `rclone md5sum` (or `md5sum`) output, one
  `<md5>  <path>` per line, paths relative to the `raw_sequence_data` root (each
  path starts with the project folder).
- `--source {ingest,backfill}`: a label stored with each md5, default `backfill`.
  It does not change behaviour.
- `--full`: projects absent from the listings have their files reset to "not
  compared". Without it, only listed projects are touched.
- `--force`: load even when a listing has fewer than half the entries expected.
  Without it the command stops and changes nothing.
- Details: [admin guide, checksums](../admin-guide.md#checksums).

## validate

```
usage: seqledger validate [-h] [--db DB] [--debug] [-v]
                          [--seqdata-root SEQDATA_ROOT]

Re-check the catalog (data-files + checksum). Exits 1 on missing/orphan files
or md5 mismatches.

options:
  -h, --help            show this help message and exit
  --db DB               path to catalog SQLite DB
  --debug               show the full traceback on error
  -v, --verbose         more detail: -v per project, -vv per file
  --seqdata-root SEQDATA_ROOT
                        root of raw_sequence_data (overrides the configured or
                        stored root)
```

- Runs the data-files check whether or not `--seqdata-root` is given. Root: the
  flag, else the configured `seqdata_root`, else each project's own stored root.
- Reports projects with missing or extra files, md5 mismatches, UniqIDs shared
  with other projects, and projects whose backup is not yet verified.
- Details: [admin guide, validate](../admin-guide.md#validate).

## integrity

```
usage: seqledger integrity [-h] [--db DB] [--debug] [-v]
                           [--seqdata-root SEQDATA_ROOT] [--project PROJECT]
                           [--jobs JOBS] [--force] [--batch]
                           [--batch-dir BATCH_DIR] [--slots SLOTS] [--mem MEM]
                           [--no-submit] [--only-unchecked] [--emit-json PATH]
                           [--collect DIR]

gzip + FASTQ integrity check of cataloged files. Exits 1 on any gzip/format
error, 2 if --batch qsub is missing or fails.

options:
  -h, --help            show this help message and exit
  --db DB               path to catalog SQLite DB
  --debug               show the full traceback on error
  -v, --verbose         more detail: -v per project, -vv per file
  --seqdata-root SEQDATA_ROOT
                        root of raw_sequence_data (overrides the configured
                        seqdata_root, which wins over each project's stored
                        root)
  --project PROJECT     limit to one project_id
  --jobs JOBS           concurrent read streams (default: min(8, CPU count);
                        on a network mount try 16-32 to fill the pipe)
  --force               re-read every file, even ones that already passed and
                        are unchanged (default: skip those for a fast,
                        resumable run)
  --batch               generate a per-project qsub script and submit it to
                        the configured I/O queue (default lTIO.sq) for remote
                        checking (the only way to reach the NAS/Store
                        partition from a compute node); each job writes JSON
                        results to merge later with --collect. Respects
                        --project.
  --batch-dir BATCH_DIR
                        dir for generated batch scripts/logs/results (default:
                        ./integrity_batch)
  --slots SLOTS         mthread slots per batch job, also the remote --jobs
                        (default 4; lTIO caps 6 slots/user and 2 concurrent
                        jobs)
  --mem MEM             memory (GB) requested per slot for batch jobs (default
                        2; lTIO caps 8G/slot)
  --no-submit           with --batch, write the qsub scripts but do not run
                        qsub
  --only-unchecked      with --batch, skip submitting a job for any project
                        whose files have all been checked before (no never-
                        checked file); avoids no-op jobs on the queue. Ignored
                        with --force. Note each job already skips unchanged
                        already-passed files.
  --emit-json PATH      (used by batch jobs) check one --project and write
                        results to PATH as JSON instead of writing the DB
  --collect DIR         merge batch result JSON files from DIR into the
                        catalog and print the per-project summary
```

- Root: the flag, else the configured `seqdata_root`, else each project's own
  stored root. (The help text says "each project's stored root", but a configured
  root wins.)
- `--batch` submits to the configured `io_queue` (default `lTIO.sq`), using the
  configured `conda_env`. Scripts go to `<batch-dir>/scripts/`, logs to
  `<batch-dir>/logs/`, results to `<batch-dir>/results/`.
- `--collect DIR` merges only results with a `.done` marker.
- `--emit-json` is used inside batch jobs; you do not need it.
- A local run saves results as it goes; Ctrl-C keeps what was checked.
- Details: [admin guide, integrity](../admin-guide.md#integrity).

## taxonomy resolve

```
usage: seqledger taxonomy resolve [-h] [--db DB] [--debug] [-v]
                                  [--source {ncbi,worms,both}]
                                  [--taxdir TAXDIR] [--force-download]
                                  [--rebuild-index] [--refresh-unconfirmed]
                                  [--redo]

Resolve taxa (NCBI taxdump and/or WoRMS) and write a review CSV next to the
catalog.

options:
  -h, --help            show this help message and exit
  --db DB               path to catalog SQLite DB
  --debug               show the full traceback on error
  -v, --verbose         more detail: -v per project, -vv per file
  --source {ncbi,worms,both}
                        taxonomy source: ncbi (default, local taxdump), worms
                        (WoRMS REST API), or both
  --taxdir TAXDIR       taxdump/cache dir (default: <db dir>/.taxonomy)
  --force-download      re-download the taxdump
  --rebuild-index       rebuild the taxdump index
  --refresh-unconfirmed
                        also re-resolve taxa resolved before but not yet
                        confirmed (default: only taxa never checked against
                        the source)
  --redo                re-resolve every taxon; confirmed taxa are kept (their
                        machine result goes to the review CSV only)
```

- Writes `taxonomy_review.csv` / `worms_review.csv` next to the catalog, or a
  dated name if that file exists, and prints the path when rows need a check.
- `--force-download` also rebuilds the index.
- WoRMS needs internet; if it cannot be reached the command prints a warning and
  carries on with NCBI (for `both`).
- Details: [admin guide, taxonomy](../admin-guide.md#taxonomy).

## taxonomy apply

```
usage: seqledger taxonomy apply [-h] [--db DB] [--debug] [-v] --review REVIEW
                                [--source {ncbi,worms}] [--taxdir TAXDIR]

Apply confirmed overrides from an edited review CSV.

options:
  -h, --help            show this help message and exit
  --db DB               path to catalog SQLite DB
  --debug               show the full traceback on error
  -v, --verbose         more detail: -v per project, -vv per file
  --review REVIEW       edited review CSV, written next to the catalog by
                        `resolve` (taxonomy_review.csv or worms_review.csv;
                        dated, e.g. taxonomy_review_YYYY-MM-DD.csv, if one
                        already exists)
  --source {ncbi,worms}
                        which review CSV this is: ncbi (confirmed_taxid,
                        default) or worms (confirmed_aphia_id)
  --taxdir TAXDIR       taxdump/cache dir (default: <db dir>/.taxonomy)
```

- `--review`: the path printed by `resolve` (the CSV lives next to the catalog,
  not in the current directory).
- Applies every row whose `confirmed_taxid` / `confirmed_aphia_id` is filled in,
  including the pre-filled exact matches. Rows with an invalid or unknown ID are
  skipped and listed.

## query

```
usage: seqledger query [-h] [--db DB] [--debug] [-v]
                       {uniq-id,search,unbacked,mismatches,summary,taxa}
                       [term]

Read-only lookups; never changes the catalog.

positional arguments:
  {uniq-id,search,unbacked,mismatches,summary,taxa}
                        lookup to run (listed below)
  term                  search term for uniq-id / search

options:
  -h, --help            show this help message and exit
  --db DB               path to catalog SQLite DB
  --debug               show the full traceback on error
  -v, --verbose         more detail: -v per project, -vv per file

lookups:
  uniq-id TERM  samples with exactly this UniqID
  search TERM   samples whose ID, taxon, or UniqID contains TERM
  unbacked      projects with no verified P-drive backup
  mismatches    files whose Store and P-drive md5 differ
  summary       one row per project (samples, files, checks, owner)
  taxa          taxa without an exact/confirmed NCBI match

columns:
  verified           1 if every file matched its P-drive copy
  data_check_status  last files-on-disk check: ok, issues, or unchecked
  n_mismatch         files whose Store and P-drive md5 differ
  n_uncompared       files whose backup is not yet verified
  owner_name         owner of the project's files on disk

Output is tab-separated; "(none)" goes to stderr when nothing matches.
```

- Read-only, never creates or changes the catalog.
- `search` matches part of the sample ID, taxon (as typed in the mapfile), or
  UniqID, case-insensitively. It does not search the NCBI lineage; use the GUI for
  that.
- `uniq-id` needs the exact UniqID, including spaces: `query uniq-id "USNM 477715"`.
- Columns per lookup:

| Lookup | Columns |
|---|---|
| uniq-id, search | project_id, sample_id, taxon, uniq_id |
| unbacked | project_id, verified, n_files, n_mismatch |
| mismatches | project_id, filename, store_md5, pdrive_md5 |
| summary | project_id, source, n_samples, n_files, data_check_status, n_mismatch, n_uncompared, owner_name |
| taxa | taxon, match_type, taxid, sci_name, rank |

## gui

```
usage: seqledger gui [-h] [--db DB] [--debug] [-v] [--port PORT]
                     [--login-host LOGIN_HOST] [--qsub] [--queue QUEUE]
                     [--conda-env CONDA_ENV] [--mem MEM] [--wait WAIT]

Launch the Streamlit browse GUI and print the SSH tunnel command.

options:
  -h, --help            show this help message and exit
  --db DB               path to catalog SQLite DB
  --debug               show the full traceback on error
  -v, --verbose         more detail: -v per project, -vv per file
  --port PORT           server port (default 8501; a free port is chosen
                        automatically under --qsub)
  --login-host LOGIN_HOST
                        Hydra login host for the SSH tunnel (default: catalog
                        config)
  --qsub                run the GUI as a job on the configured I/O queue
                        (default lTIO.sq) so it reads the master catalog on
                        Store directly (no Scratch copy); waits for it to
                        start, then prints the tunnel command to screen
  --queue QUEUE         queue for --qsub (default: catalog config, else
                        lTIO.sq)
  --conda-env CONDA_ENV
                        conda env activated inside the --qsub job (default:
                        catalog config)
  --mem MEM             memory GB for the --qsub job (default 6)
  --wait WAIT           seconds to wait for the --qsub GUI to start serving
                        (default 300)
```

- Without `--qsub`: runs the GUI on the current machine on `--port` (default
  8501; if that port is busy, a free one is used and printed). On a Hydra node it
  prints the SSH tunnel command; off the cluster it prints only the
  `http://localhost:<port>` link. Ctrl-C stops it.
- With `--qsub`: submits a job, waits up to `--wait` seconds for it to serve,
  then prints the tunnel command, the job id, the log path, and the `qdel`
  command. The job ends after 72 hours (lTIO limit).
- `--login-host`, `--queue`, `--conda-env`: the flag, else the catalog config,
  else the built-in default.
- The GUI only listens on 127.0.0.1; reach it through the tunnel.
- Details: [user guide](../user-guide.md#start-the-gui).

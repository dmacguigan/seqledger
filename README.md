# seqledger

A SQLite-backed catalog + Python tooling that becomes the source of truth for a
lab's raw sequence (FASTQ) data on an HPC cluster. It adds schema validation,
per-file gzip/FASTQ integrity checks, `rclone` checksum + backup verification, and
a queryable central index with a lightweight read-only browse GUI. Built for the
NMNH "Ocean DNA" project on Hydra, and configurable (`init-db` config) to retarget
another lab's paths, cluster settings, and catalog name.

> Prototype, in active development. Per-deployment settings (catalog display name,
> seqdata/metadata roots, conda env, login host, I/O queue, rclone module, FASTQ
> extensions) are set once at `init-db` — see the [Configuration](#configuration) section.

## Who this is for

There are two kinds of user. Most people are the first kind.

| | **Users** — browse only | **Data managers** — run the catalog |
|---|---|---|
| **What you do** | search/filter the catalog, view sample & file info, export CSV/[MitoPilot](https://github.com/Smithsonian/MitoPilot) map files, build copy jobs in **Grab & Go** | everything Users do, **plus** create the catalog, ingest metadata, run integrity + checksums, resolve taxonomy, keep it current |
| **How** | log into the cluster, run **one** `seqledger … gui --qsub` command to start the **read-only browse GUI**, open the printed link in a web browser | the full `seqledger` command-line tool |
| **Install anything?** | **Once**, into your own account — a copy-paste conda + pip setup (no admin, no shared env, no editing files). Steps in [For GUI users](#for-gui-users-browsing-only). | Yes — same install (see [Install](#install)). |
| **Start here** | [**For GUI users (browsing only)**](#for-gui-users-browsing-only) | [Requirements](#requirements) → [Quick start](#quick-start) → [Usage](#usage) |

## For GUI users (browsing only)

You install seqledger once into your **own** Hydra account, then launch the GUI
yourself whenever you want to browse. Ask your data manager once for the **catalog
path** for your lab (the `.db` file on Store) — that's the only thing you need from
them.

### One-time setup (on Hydra)

Log into the login node and create the `seqledger` conda environment. This installs
into *your* account — no admin or shared environment needed.

```bash
ssh <you>@hydra-login01.si.edu
git clone https://github.com/OWNER/seqledger.git
cd seqledger
conda env create -f environment.yml    # creates an env named 'seqledger'
conda activate seqledger
pip install -e '.[gui]'
```

> Keep the environment name **`seqledger`** (the default). The GUI job activates the
> env by that name; if you must use a different name, see [Install](#install).

### Each time you want to browse

**On the cluster** — log in, activate the env, and start the GUI:

```bash
ssh <you>@hydra-login01.si.edu
conda activate seqledger
seqledger --db <CATALOG_PATH> gui --qsub
```

`--qsub` starts the GUI on the cluster's I/O queue (so it reads the catalog
directly) and, once it's running, **prints two things to your screen**: an
`ssh -N -L …` tunnel command and a `http://localhost:<port>` link. Leave this
window open.

**On your own computer** — open a *second* terminal and paste the printed tunnel
command (it looks like nothing happens — that's correct; leave it open):

```bash
ssh -N -L <port>:<node>:<port> <you>@hydra-login01.si.edu
```

Then open the printed **`http://localhost:<port>`** link in your web browser.

**When you're done**, back in the first (cluster) terminal run the `qdel <job>`
command it printed, to stop the GUI. (It also stops on its own after 72 hours.)

Inside the GUI (pick a view in the left sidebar):

- **Projects / Samples / Files** — search, filter, and download the table as CSV.
- **Taxonomy** — an interactive sunburst of the catalog's taxonomic breadth.
- **Grab & Go** — build a custom set of samples, export a CSV or a [**MitoPilot**](https://github.com/Smithsonian/MitoPilot)
  map file, and generate a ready-to-run copy job for their sequence data.

Everything is **read-only** — you can't change or delete catalog data from the GUI.
The sections below are for **data managers**.

> **Tip:** set `export SEQLEDGER_DB=<CATALOG_PATH>` in your `~/.bashrc` so you can
> drop `--db` and just run `seqledger gui --qsub`.

## Requirements

- **Python 3.10+** for the core CLI (standard library only — runs on a Hydra login
  node or a Mac with no install step).
- **`rclone`** on `PATH` (or `module load`) for the `checksums` step.
- **conda** with a `seqledger` env (`environment.yml`) only for the **GUI**
  (`streamlit` + `pandas` + `plotly`).
- Back up the catalog `.db` before running `init-db` on an existing catalog —
  migrations add columns in place and are not reversible.

## Quick start

Placeholders in `<ANGLE_BRACKETS>` are yours to fill in (the Ocean-DNA values in
the examples further down are just one lab's settings).

```bash
# 1. create a catalog + set this deployment's config (all flags optional)
seqledger --db <YOUR_CATALOG>.db init-db \
    --name "<Your Lab> sequence catalog" --slug <yourlab> \
    --seqdata-root <SEQDATA_ROOT> --metadata-root <METADATA_ROOT> \
    --conda-env <ENV> --login-host <LOGIN_HOST>

# 2. ingest: auto-discover project folders + their <project>_mapfile.csv.
#    The roots come from the config set in step 1, so no need to repeat them
#    (pass --seqdata-root/--metadata-root to override for a one-off run).
seqledger --db <YOUR_CATALOG>.db ingest

# 3. browse it (serve on the I/O queue so it reads the master on Store directly)
seqledger --db <YOUR_CATALOG>.db gui --qsub
```
Integrity, checksums, taxonomy, and query are separate steps — see Usage below.

## Why

The old flow kept one CSV per project and validated with `validate_seq_data.py`.
Gaps this addresses:

- **No content integrity** - `rclone` only compared size + mtime, so silent
  corruption / truncated backups went undetected. We now record and compare md5s.
- **Fragile matching** - the old "is this fastq in the metadata" test was a
  substring search over raw CSV text (`sample_1.fastq.gz` matched
  `sample_11.fastq.gz`). We match by exact basename, both directions.
- **No schema enforcement** - now: required non-null fields, `R1 != R2`, unique
  sample IDs per project, cross-project UniqID duplicate detection.
- **No central index** - now one queryable DB ("where is USNM 477715?", "which
  projects are not verified-backed-up?").

## Design at a glance

- **Backend:** one SQLite DB. Master lives on Store (backed up like the data). The
  catalog is only metadata + checksums (a few MB), decoupled from the huge FASTQs.
  **Store is not mounted on compute nodes**, so the GUI reaches the master one of
  two ways: preferred, run the GUI as a job on the **I/O queue (`gui --qsub`)**,
  which *can* read Store directly; or serve it from a **read-only copy synced to
  Scratch** (which compute nodes do mount).
- **Metadata entry:** unchanged - users submit the CSV map file.
- **Checksums:** captured from `rclone md5sum` run on **both Store and P-drive**,
  then compared. Piggybacks on the backup the data manager already runs; no hashing
  burden on users. The same step backfills existing already-backed-up data (a
  one-time catch-up that doubles as the first real backup audit).
- **Frontend:** read-only Streamlit GUI served on Hydra over an SSH tunnel

Core tooling (ingest / checksums / validate / query) uses only the Python stdlib,
so it runs on a Hydra login node or a Mac with no install step. Only the GUI needs
`streamlit` + `pandas`, provided as a conda env (`environment.yml`; `requirements.txt`
kept for pip users).

## Install

Everyone installs into their own account (there's no shared environment). On Hydra,
use the conda env so the GUI deps are present:

```bash
git clone https://github.com/OWNER/seqledger.git
cd seqledger
conda env create -f environment.yml    # creates an env named 'seqledger'
conda activate seqledger
pip install -e '.[gui]'                 # GUI + CLI; use -e . for CLI only
```

Runs without installing too: `python -m seqledger ...` from the repo root. On a Mac
the stdlib-only CLI needs no install at all.

### Conda environment name

The env name comes from the `name:` field in `environment.yml` (`seqledger`), so
`conda env create -f environment.yml` makes an env called **`seqledger`**. Override
it with `conda env create -n <name> -f environment.yml`.

**Keep the default `seqledger` unless you have a reason not to.** The `gui --qsub`
and `integrity --batch` job scripts run `conda activate <conda_env>` on the compute
node, where `<conda_env>` is the catalog's `conda_env` [config](#configuration)
value (default `seqledger`). Your env name must match it. If a deployment uses a
different name, either create your env with that name, or pass `--conda-env <name>`
(and, for the whole catalog, set it once with
`seqledger --db … init-db --conda-env <name>`).

## Layout

```
pyproject.toml           packaging (the `seqledger` console command)
seqledger/               the package
  cli.py                 CLI entry point (subcommands); `python -m seqledger`
  db.py ingest.py checksums.py validate.py integrity.py taxonomy.py query.py
  rclone.py              rclone copy-job builder
  mitopilot.py           [MitoPilot](https://github.com/Smithsonian/MitoPilot) map-file export
  gui.py                 GUI launcher (local or `--qsub` on the I/O queue)
  schema.sql             SQLite DDL (source of truth for tables)
  app/streamlit_app.py   read-only browse GUI
tests/                   pytest suite + fixture builders
```

## Usage

```bash
# 1. create the catalog
seqledger --db oceandna_catalog.db init-db

# 2. ingest metadata -- AUTO-DISCOVERY (recommended): point at the sequence-data
#    root and the metadata root; no map file needed. Every top-level folder in
#    --seqdata-root is a project, paired with '<project>_mapfile.csv' in
#    --metadata-root. FASTQ files may be nested in subdirs -- they are found
#    recursively. Projects are always added; pairing problems are flagged on the
#    project row (metadata_status, shown in the GUI 'mapfile' column):
#      missing_mapfile  folder on disk, no mapfile -> files cataloged, no samples
#      missing_seqdata  mapfile present, no folder  -> samples cataloged, no files
#      broken_mapfile   mapfile header malformed    -> files cataloged, no samples
#    (an 'ok' project has a parseable mapfile and a matching folder).
#
#    ingest is self-driving: after loading rows it auto-runs the taxonomy-resolve
#    step (5 below) + a data-files check on whatever is new or changed. It upserts,
#    so re-running picks up added/fixed mapfiles (a missing_mapfile project flips to
#    ok once its mapfile appears) and re-resolves changed names. These steps are
#    gated so unchanged re-runs are cheap. Samples dropped from a CSV are reported
#    but kept, not pruned (use --prune to remove them + stale file rows). A project
#    that vanished from BOTH roots (folder + mapfile deleted) lingers unless you add
#    --prune-projects, which deletes it (cascading to its samples/files); that step
#    refuses to run if the roots turn up no projects, so a missing mount can't wipe
#    the catalog. (Deleting only a mapfile flips the project to missing_mapfile and
#    leaves its old samples in place.) Use --skip-taxonomy to load metadata only.
#    NOTE: the integrity byte-scan (4b) is NOT run by ingest -- it is slow, so run
#    it separately when ready.
seqledger --db oceandna_catalog.db ingest \
    --metadata-root ../raw_sequence_metadata \
    --seqdata-root /store/nmnh_ocean_dna/public/raw_sequence_data

#    ingest metadata -- MANUAL (override for odd layouts): pass an explicit
#    two-column map file (metadata csv, data dir), same format as
#    scripts/.../example_map_file.txt. Per-project CSVs are looked up in
#    --metadata-root (default: the map file's own dir).
seqledger --db oceandna_catalog.db ingest map_file.txt \
    --metadata-root ../raw_sequence_metadata \
    --seqdata-root /store/nmnh_ocean_dna/public/raw_sequence_data

# 3. checksums: run rclone md5sum on BOTH sides, then load + compare
rclone md5sum SI-Hydra:/store/nmnh_ocean_dna/public/raw_sequence_data > store.md5
rclone md5sum /Volumes/nmnh-ocean-dna/Hydra_backup/store/raw_sequence_data > pdrive.md5
seqledger --db oceandna_catalog.db checksums --store store.md5 --pdrive pdrive.md5
#   add --source ingest for new data, --project X to scope to one project

# 4. re-check the whole catalog: two per-project results
#    - data-files: reciprocal mapfile <-> disk (missing files + orphans);
#      needs --seqdata-root to scan disk, and is persisted to `projects`.
#    - checksum: Store vs P-drive md5 (from the `checksums` step above).
seqledger --db oceandna_catalog.db validate \
    --seqdata-root /store/nmnh_ocean_dna/public/raw_sequence_data

# 4b. integrity: gzip + FASTQ structural check of cataloged files.
#    Stream-decompresses each *.fastq.gz to EOF (== `gzip -t`, catches truncation
#    / CRC corruption), validates the FASTQ 4-line record structure, and compares
#    R1/R2 read counts per sample. Per-file results land in `files`
#    (integrity_status, gz_ok, n_reads); a per-project run status is logged to
#    `validation_log`. Reads every byte, so it is a separate opt-in step; files
#    are checked concurrently (--jobs, default min(8, CPU count)).
seqledger --db oceandna_catalog.db integrity \
    --seqdata-root /store/nmnh_ocean_dna/public/raw_sequence_data
#    (--seqdata-root is optional; each project's ingest-time root is used by default)

# 4b-batch. integrity --batch: run the check on Hydra's I/O queue (lTIO.sq).
#    The Store/NAS partition is only reachable from a compute node via the I/O
#    queue, so --batch generates one qsub script per project and submits it there
#    (respects --project to limit to one). Each remote job checks its project and
#    writes results to <batch-dir>/results/<project>.json instead of the DB -- no
#    two Hydra nodes ever write the shared SQLite catalog at once. The env inside
#    each job is `source ~/.bashrc; conda activate seqledger`.
#    Tunables: --slots (mthread slots + remote --jobs, default 4), --mem (GB/slot,
#    default 2), --batch-dir (default ./integrity_batch), --no-submit (write the
#    scripts but don't qsub). Note lTIO caps: 6 slots/user, 2 concurrent jobs,
#    8G/slot, 72h wall -- with the default 4 slots a 2nd concurrent job queues.
seqledger --db oceandna_catalog.db integrity --batch \
    --seqdata-root /store/nmnh_ocean_dna/public/raw_sequence_data
#    Once the jobs finish, merge their JSON results back into the catalog (this is
#    the only step that writes the DB; runs locally + serially, then aggregates
#    per-project summaries + validation_log the same as a live run):
seqledger --db oceandna_catalog.db integrity --collect integrity_batch/results
#    Checkpointed + resumable per project: each job flushes its results JSON every
#    ~200 checked files and, on restart, reloads its own JSON and skips already-done
#    files -- so a job killed at the lTIO 72h wall / 12h-per-slot CPU cap resumes
#    where it stopped when you just re-qsub it (no re-reading). --force re-reads all.
#    Incremental skip also applies: a re-submitted job reads prior gz_ok/size
#    from the DB and skips unchanged files that already passed. To also avoid
#    submitting a no-op job for a project that is already fully checked, add
#    --only-unchecked (skips projects with no never-checked file; ignored under
#    --force):
seqledger --db oceandna_catalog.db integrity --batch --only-unchecked \
    --seqdata-root /store/nmnh_ocean_dna/public/raw_sequence_data

# 5. taxonomy: resolve free-text Taxon -> NCBI TaxID + lineage
#    Downloads a pinned NCBI taxdump into <db dir>/.taxonomy (once), indexes it,
#    resolves distinct sample Taxa (exact, then genus-anchored fuzzy), writes the
#    results into the `taxa` table + a review CSV. By DEFAULT it resolves only taxa
#    with no `taxa` row yet (never checked against NCBI), so a new ingest doesn't
#    re-run every taxon. Add --refresh-unconfirmed to also re-resolve taxa resolved
#    before but not yet confirmed, or --redo to re-resolve everything.
#    NOTE: if you built a taxdump index before this version, rebuild it once so
#    the new tax_names(taxid) index (large speed-up) is applied:
#        seqledger --db oceandna_catalog.db taxonomy resolve --rebuild-index
seqledger --db oceandna_catalog.db taxonomy resolve
#    edit confirmed_taxid in taxonomy_review.csv for any wrong fuzzy/unresolved
#    rows, then fold the overrides back in:
seqledger --db oceandna_catalog.db taxonomy apply --review taxonomy_review.csv

# 6. lookups
seqledger --db oceandna_catalog.db query summary
seqledger --db oceandna_catalog.db query uniq-id "USNM 477715"
seqledger --db oceandna_catalog.db query search Urophycis
seqledger --db oceandna_catalog.db query unbacked
seqledger --db oceandna_catalog.db query mismatches
seqledger --db oceandna_catalog.db query taxa      # fuzzy/unresolved taxa
```

### Backfill of existing data

Same `checksums` command with `--source backfill` (the default). Run it per project
(`--project ...`) in the background so you don't monopolize Store read bandwidth.

### GUI (over SSH tunnel)

One-time env setup on Hydra:

```bash
conda env create -f environment.yml     # creates the `seqledger` env
conda activate seqledger                 # or: mamba env create -f environment.yml
```
(Existing env? `conda env update -f environment.yml`. Pip users: `pip install -r
requirements.txt`.)

**Recommended -- serve it on the I/O queue** (reads the master on Store directly,
no Scratch copy). From a login node:

```bash
seqledger --db /store/nmnh_ocean_dna/public/oceandna_catalog.db gui --qsub
```
This submits a Streamlit job to `lTIO.sq`, waits for it to start, then prints the
exact `ssh -N -L <port>:<node>:<port> you@hydra-login01.si.edu` command to the
screen (no digging through the job log). Run that on your local computer, open
`http://localhost:<port>`, and `qdel <job>` when done. lTIO caps jobs at 72h wall
and 2 concurrent jobs/user, so the GUI stops after 72h -- just resubmit.

**Alternative -- run it directly** on an interactive node against a Scratch copy of
the DB (compute nodes don't mount Store):

```bash
seqledger --db /scratch/nmnh_ocean_dna/oceandna_catalog.db gui --port 8501
```
This prints the `ssh -N -L 8501:NODE:8501 you@hydra-login01.si.edu` command; run it
locally, then open `http://localhost:8501`. Read-only browsing plus a
build-your-own selection view (sidebar):

- **Projects** - per-project summary stats plus check fields: `mapfile`
  (folder<->mapfile pairing: OK / no mapfile / no data folder / broken mapfile),
  `data_files` (mapfile <-> disk reciprocal: OK / "N missing, M orphan"), and
  `checksum` (Store vs P-drive: verified / "N mismatch" / incomplete). Filter to
  just the flagged ones ("Only mapfile issues" etc). Select a project row to see
  its mapfile explanation + drill into its data_files issues (each missing /
  orphan file, with sample info where known).
- **Samples** - one row per sample; the on-screen table stays lean, but the CSV
  export carries the full absolute R1/R2 paths + owner.
- **Files** - one row per FASTQ: full absolute path, size, owner, backup status.
- **Taxonomy** - interactive Plotly sunburst of the catalog's taxonomic breadth
  (filter by project, pick the deepest rank, toggle the `unknown` bucket) plus a
  per-rank sample-count bar chart. Populated by `taxonomy resolve`.
- **Grab & Go** - build a hand-picked set of samples. Search by project,
  taxonomy (taxon / NCBI name / lineage), sample ID, and UniqID, with a **regex
  toggle**; add matches (selected rows or all) to a running table that persists as
  you refine the search. Export the table to CSV or as a **[MitoPilot](https://github.com/Smithsonian/MitoPilot) map file**
  (ID, R1, R2, Taxon -- R1/R2 are filenames; pick which column fills the `ID`),
  and generate an **rclone copy job** for the selected samples' sequence data: a
  self-contained `lTIO.sq`
  submission script (`module load tools/rclone/1.66.0`, one `rclone copy
  --files-from` per source root, preserving the directory layout) with a
  disk-space estimate. Copy it from the screen or download the `.job`, then
  `qsub` it on the login node.

The Samples view also links each row to its NCBI datasets taxonomy browser page.
Each view filters/searches and downloads CSV.

## Configuration

Per-deployment settings live in a `config` table in the catalog DB, set at
`init-db` (re-run it any time to change them; unset keys keep sensible defaults, so
an existing catalog is unchanged). Because the GUI reads a copy of the DB, config
travels with it; generated qsub/rclone scripts bake the values in.

```bash
seqledger --db catalog.db init-db --show          # print resolved config
seqledger --db catalog.db init-db --name "..." --conda-env myenv
seqledger --db catalog.db init-db --set io_queue=sThM.q   # any key
```

| key | what it controls | default |
|---|---|---|
| `catalog_name` / `catalog_slug` | GUI title + CLI banner / export-file prefix | Ocean DNA … / `oceandna` |
| `seqdata_root` / `metadata_root` | default roots for `ingest` / `validate` / `integrity` — set these once and you can omit `--seqdata-root` / `--metadata-root` on every run (an explicit flag still overrides) | (unset) |
| `conda_env` | env activated inside generated qsub jobs | `seqledger` |
| `login_host` | Hydra login host in GUI tunnel commands | `hydra-login01.si.edu` |
| `io_queue` | queue for `integrity --batch`, `gui --qsub`, rclone jobs | `lTIO.sq` |
| `rclone_module` | `module load`ed in rclone copy jobs | `tools/rclone/1.66.0` |
| `backup_location` | "verified backup" location label | `pdrive` |
| `fastq_extensions` | FASTQ suffixes discovered on disk | `fastq.gz,fq.gz` |

## Troubleshooting

| you see | it means / what to do |
|---|---|
| `ingest` prints `WARNING: discovered 0 projects` | the roots are empty/unmounted/mistyped, or no `<project>_mapfile.csv` files — check the paths and that Store/NAS is mounted. |
| Projects view `mapfile` = `no mapfile` / `broken mapfile` / `no data folder` | a project folder has no (or a malformed) mapfile, or a mapfile has no folder. Select the row for the full explanation; the files are still cataloged. |
| `data_files` = "N missing / M orphan" | files listed in the mapfile aren't on disk (missing), or on-disk FASTQ aren't in the mapfile (orphan). |
| a batch integrity/copy job "fails" near 72h | it hit the lTIO wall/CPU cap; the per-project checkpoint is saved — just **re-`qsub`** (integrity resumes; rclone `--checksum` skips copied files). |
| `integrity --collect` skips a file "no .done marker" | that project's job is still running or was killed — let it finish (or resubmit), then re-collect. |
| GUI: tunnel connects but browser shows "connection refused" | the Streamlit server didn't start — usually the conda env wasn't activated, or (with `--qsub`) the job is still starting; check the printed job log. |
| a CLI command prints a one-line `error:` | re-run with `--debug` to see the full traceback. |

## Schema

`projects` (1 per sequencing project; `metadata_status` / `metadata_detail` record
folder<->mapfile pairing health from auto-discovery ingest) -> `samples` (1 per
sample, extra map-file columns kept as JSON) -> `files` (R1/R2, with `store_md5` /
`pdrive_md5` / `md5_match`; `rel_path` may be nested when FASTQ live in subdirs).
`backups` summarizes per-project verification; `validation_log` records
validation runs. `taxa` holds the NCBI resolution per distinct raw Taxon
string (taxid, ranked lineage columns `tax_domain`..`tax_species`, `match_type`,
`confirmed`), joined to `samples.taxon`. See `schema.sql`.

Taxonomy resolution is pure-Python (stdlib): the NCBI taxdump is parsed once into
`<db dir>/.taxonomy/taxdump.sqlite` (git-ignored), then exact `name -> taxid` with
a genus-anchored fuzzy fallback. Mirrors RiboPilot's `R/taxonomy.R` approach
without the `taxonkit` binary.

Ownership + size (`owner_name` / `owner_uid` / `size_bytes` on `files`, plus
`owner_name` / `seqdata_root` on `projects`) are captured during `ingest` when
`--seqdata-root` is reachable; re-run ingest to refresh. The data-files check result
(`data_check_status` / `data_check_n_missing` / `data_check_n_orphan` /
`data_check_date` on `projects`) is written by `validate --seqdata-root`; the same run
rewrites the per-file `data_check_issues` table (missing / orphan filenames) that backs
the GUI drill-down. The checksum result is derived from `files.md5_match`. `init-db`
auto-migrates older catalogs by adding the new columns.

## Open items

- Confirm the fifth map-file column name against a live map file. The guide says
  `UniqID`; the old script checked `UniqueID`. The validator currently accepts
  either (`seqledger/db.py: UNIQID_ALIASES`); pin it once confirmed.
- Decide the canonical Store/Scratch paths for the master DB and its GUI copy.

## Tests

```bash
python -m pytest tests/ -q
```

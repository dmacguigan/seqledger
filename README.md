# Ocean DNA raw sequence catalog

A SQLite-backed catalog + Python tooling that replaces the per-project CSV "map
files" as the source of truth for Ocean DNA raw sequence (FASTQ) data management.
Users keep submitting the same CSV map files; the catalog adds schema validation,
per-file checksums, backup verification, and a queryable central index with a
lightweight browse GUI.

> Prototype. Lives in a git-ignored directory. Does not yet replace the published
> data management guide (`qmd/datamanagement.qmd`).

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

- **Backend:** one SQLite DB. Master lives on Store (backed up like the data); a
  synced read-only copy on Scratch feeds the GUI, since **Store is not mounted on
  compute nodes** but Scratch is. The catalog is only metadata + checksums (a few
  MB), decoupled from the huge FASTQs.
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

## Layout

```
schema.sql            SQLite DDL (source of truth for tables)
odna.py               CLI entry point
odna/                 package: db, ingest, checksums, validate, integrity, taxonomy, query, gui
app/streamlit_app.py  read-only browse GUI
tests/                pytest suite + fixture builders
```

## Usage

```bash
# 1. create the catalog
python odna.py --db oceandna_catalog.db init-db

# 2. ingest metadata from a two-column map file (metadata csv, data dir),
#    same format as scripts/.../example_map_file.txt. Per-project CSVs are
#    looked up in --metadata-root (default: the map file's own dir).
#    --seqdata-root enables on-disk R1/R2 + orphan checks when data is reachable.
#
#    ingest is self-driving: after loading rows it auto-runs the integrity and
#    taxonomy-resolve steps (4b + 5 below) on whatever is new or changed. It
#    upserts, so re-running on an edited CSV updates the rows (Taxon edits
#    included) and re-resolves any changed name. The follow-on steps are gated
#    so unchanged re-runs are cheap: integrity only touches files it has never
#    checked (and needs --seqdata-root to reach them on disk), taxonomy only
#    runs when a genuinely new Taxon string appears. Samples dropped from a CSV
#    are reported but kept, not pruned. Use --skip-integrity / --skip-taxonomy
#    to load metadata only; checksums stay separate (they need rclone output)
#    and `taxonomy apply` stays a manual review step.
python odna.py --db oceandna_catalog.db ingest map_file.txt \
    --metadata-root ../raw_sequence_metadata \
    --seqdata-root /store/nmnh_ocean_dna/public/raw_sequence_data

# 3. checksums: run rclone md5sum on BOTH sides, then load + compare
rclone md5sum SI-Hydra:/store/nmnh_ocean_dna/public/raw_sequence_data > store.md5
rclone md5sum /Volumes/nmnh-ocean-dna/Hydra_backup/store/raw_sequence_data > pdrive.md5
python odna.py --db oceandna_catalog.db checksums --store store.md5 --pdrive pdrive.md5
#   add --source ingest for new data, --project X to scope to one project

# 4. re-check the whole catalog: two per-project results
#    - data-files: reciprocal mapfile <-> disk (missing files + orphans);
#      needs --seqdata-root to scan disk, and is persisted to `projects`.
#    - checksum: Store vs P-drive md5 (from the `checksums` step above).
python odna.py --db oceandna_catalog.db validate \
    --seqdata-root /store/nmnh_ocean_dna/public/raw_sequence_data

# 4b. integrity: gzip + FASTQ structural check of cataloged files.
#    Stream-decompresses each *.fastq.gz to EOF (== `gzip -t`, catches truncation
#    / CRC corruption), validates the FASTQ 4-line record structure, and compares
#    R1/R2 read counts per sample. Per-file results land in `files`
#    (integrity_status, gz_ok, n_reads); a per-project run status is logged to
#    `validation_log`. Reads every byte, so it is a separate opt-in step; files
#    are checked concurrently (--jobs, default min(8, CPU count)).
python odna.py --db oceandna_catalog.db integrity \
    --seqdata-root /store/nmnh_ocean_dna/public/raw_sequence_data
#    (--seqdata-root is optional; each project's ingest-time root is used by default)

# 4b-batch. integrity --batch: run the check on Hydra's I/O queue (lTIO.sq).
#    The Store/NAS partition is only reachable from a compute node via the I/O
#    queue, so --batch generates one qsub script per project and submits it there
#    (respects --project to limit to one). Each remote job checks its project and
#    writes results to <batch-dir>/results/<project>.json instead of the DB -- no
#    two Hydra nodes ever write the shared SQLite catalog at once. The env inside
#    each job is `source ~/.bashrc; conda activate odna`.
#    Tunables: --slots (mthread slots + remote --jobs, default 4), --mem (GB/slot,
#    default 2), --batch-dir (default ./integrity_batch), --no-submit (write the
#    scripts but don't qsub). Note lTIO caps: 6 slots/user, 2 concurrent jobs,
#    8G/slot, 72h wall -- with the default 4 slots a 2nd concurrent job queues.
python odna.py --db oceandna_catalog.db integrity --batch \
    --seqdata-root /store/nmnh_ocean_dna/public/raw_sequence_data
#    Once the jobs finish, merge their JSON results back into the catalog (this is
#    the only step that writes the DB; runs locally + serially, then aggregates
#    per-project summaries + validation_log the same as a live run):
python odna.py --db oceandna_catalog.db integrity --collect integrity_batch/results
#    Incremental skip still applies: a re-submitted job reads prior gz_ok/size
#    from the DB and skips unchanged files that already passed.

# 5. taxonomy: resolve free-text Taxon -> NCBI TaxID + lineage
#    Downloads a pinned NCBI taxdump into <db dir>/.taxonomy (once), indexes it,
#    resolves every distinct sample Taxon (exact, then genus-anchored fuzzy),
#    writes the results into the `taxa` table + a review CSV.
#    NOTE: if you built a taxdump index before this version, rebuild it once so
#    the new tax_names(taxid) index (large speed-up) is applied:
#        python odna.py --db oceandna_catalog.db taxonomy resolve --rebuild-index
python odna.py --db oceandna_catalog.db taxonomy resolve
#    edit confirmed_taxid in taxonomy_review.csv for any wrong fuzzy/unresolved
#    rows, then fold the overrides back in:
python odna.py --db oceandna_catalog.db taxonomy apply --review taxonomy_review.csv

# 6. lookups
python odna.py --db oceandna_catalog.db query summary
python odna.py --db oceandna_catalog.db query uniq-id "USNM 477715"
python odna.py --db oceandna_catalog.db query search Urophycis
python odna.py --db oceandna_catalog.db query unbacked
python odna.py --db oceandna_catalog.db query mismatches
python odna.py --db oceandna_catalog.db query taxa      # fuzzy/unresolved taxa
```

### Backfill of existing data

Same `checksums` command with `--source backfill` (the default). Run it per project
(`--project ...`) in the background so you don't monopolize Store read bandwidth.

### GUI (over SSH tunnel)

On Hydra (login node or `srun --pty` node), pointing at a DB copy on Scratch:

```bash
conda env create -f environment.yml     # once; creates the `odna` env
conda activate odna                      # or: mamba env create -f environment.yml
python odna.py --db /scratch/nmnh_ocean_dna/oceandna_catalog.db gui --port 8501
```

(Existing env? `conda env update -f environment.yml`. Pip users: `pip install -r
requirements.txt`.)

It prints the exact `ssh -N -L 8501:NODE:8501 you@hydra-login01.si.edu` command;
run that locally, then open `http://localhost:8501`. Read-only, three views (sidebar):

- **Samples** - one row per sample; the on-screen table stays lean, but the CSV
  export carries the full absolute R1/R2 paths + owner.
- **Projects** - per-project summary stats plus two check fields:
  `data_files` (mapfile <-> disk reciprocal: OK / "N missing, M orphan") and
  `checksum` (Store vs P-drive: verified / "N mismatch" / incomplete). Select a
  project row to drill into its data_files issues (each missing / orphan file,
  with sample info where known).
- **Files** - one row per FASTQ: full absolute path, size, owner, backup status.
- **Taxonomy** - interactive Plotly sunburst of the catalog's taxonomic breadth
  (filter by project, pick the deepest rank, toggle the `unknown` bucket) plus a
  per-rank sample-count bar chart. Populated by `taxonomy resolve`.

The Samples view also links each row to its NCBI datasets taxonomy browser page.
Each view filters/searches and downloads CSV.

## Schema

`projects` (1 per sequencing project) -> `samples` (1 per sample, extra map-file
columns kept as JSON) -> `files` (R1/R2, with `store_md5` / `pdrive_md5` /
`md5_match`). `backups` summarizes per-project verification; `validation_log`
records validation runs. `taxa` holds the NCBI resolution per distinct raw Taxon
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
  either (`odna/db.py: UNIQID_ALIASES`); pin it once confirmed.
- Decide the canonical Store/Scratch paths for the master DB and its GUI copy.

## Tests

```bash
python -m pytest tests/ -q
```

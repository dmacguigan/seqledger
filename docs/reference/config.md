# Configuration reference

Per-catalog settings: what each key does, its default, and how to set it.

Settings are stored in the `config` table inside the catalog file, so every copy
of the catalog carries them, and generated job scripts bake them in when they are
written. Set them with `init-db` (see [CLI reference](cli.md#init-db)):

```bash
seqledger --db /path/to/catalog.db init-db --name "My lab sequence catalog"
seqledger --db /path/to/catalog.db init-db --set conda_env=myenv --set io_queue=sThM.q
seqledger --db /path/to/catalog.db init-db --show
```

A key that was never set uses its default. Re-running `init-db` changes only the
keys you pass. `--set` does not check key names, so a typo is stored silently;
confirm with `--show`.

## Keys

| Key | Default | Set with | Used by | Notes |
|---|---|---|---|---|
| `catalog_name` | `Ocean DNA sequence data catalog` | `--name` | GUI page title | |
| `catalog_slug` | `oceandna` | `--set catalog_slug=` | GUI CSV download names (`<slug>_samples.csv` etc.) | Script-safe characters only (see below) |
| `seqdata_root` | (empty) | `--seqdata-root` | `ingest`, `validate`, `integrity`, batch scripts | Absolute path of the `raw_sequence_data` folder. When set, it overrides each project's stored root in `validate` and `integrity` |
| `metadata_root` | (empty) | `--metadata-root` | `ingest` | Folder holding the `<project>_mapfile.csv` files (flat) |
| `conda_env` | `seqledger` | `--set conda_env=` | `integrity --batch` jobs, `gui --qsub` job | Must match the env name in each user's account. `gui --conda-env` overrides it per run |
| `login_host` | `hydra-login01.si.edu` | `--set login_host=` | Tunnel command printed by `gui` | `gui --login-host` overrides it per run |
| `io_queue` | `lTIO.sq` | `--set io_queue=` | `integrity --batch`, `gui --qsub`, Grab & Go copy jobs | `gui --queue` overrides it per run |
| `rclone_module` | `tools/rclone/1.66.0` | `--set rclone_module=` | Grab & Go copy jobs (`module load`) | |
| `backup_location` | `pdrive` | `--set backup_location=` | `checksums`, `query unbacked`, GUI backup columns | Name of the backup row in the `backups` table. Changing it after `checksums` has run hides the old rows until `checksums` runs again |
| `fastq_extensions` | `fastq.gz,fq.gz` | `--set fastq_extensions=` | `ingest`, `validate` (which files count as FASTQs on disk) | Comma-separated. Files must be gzip-compressed: `integrity` reads them as gzip, and R1/R2 is only guessed from `.fastq.gz` / `.fq.gz` names |
| `dismiss_log` | (set by init-db) | `--set dismiss_log=` | GUI | Path of the hidden log where GUI users' dismissed issues are recorded. init-db fills it with the path next to the catalog it runs on, so copies of the catalog point back at the master's log. Change it only if the log must live elsewhere. |

## Precedence

For every setting: **command-line flag, then catalog config, then built-in
default.**

The root folders have one more level. In `validate` and `integrity` the order is:

1. `--seqdata-root` on the command line,
2. the configured `seqdata_root`,
3. the root each project was ingested from (`projects.seqdata_root`).

So a configured `seqdata_root` overrides every project's own root. If your
projects live under more than one root, clear it
(`init-db --set seqdata_root=`) and each project uses its own.

`ingest` takes `--seqdata-root` / `--metadata-root`, else the configured values,
and needs both.

The catalog path itself is not a config key: it is `--db`, else
`$SEQLEDGER_DB`, else `catalog.db` in the current directory.

## Allowed values

- No value may contain a newline, carriage return, or NUL.
- Keys that are written into job scripts (`conda_env`, `io_queue`,
  `rclone_module`, `login_host`, `catalog_slug`) may only use letters, digits,
  and `. _ @ : / -`. Anything else is refused with an error.

## Not configurable

- The first five mapfile columns are fixed: `ID,R1,R2,Taxon,UniqID` (or
  `UniqueID`). See [mapfile reference](mapfile.md).
- The mapfile name suffix, `_mapfile.csv`.
- The scheduler: jobs are SGE `qsub` scripts with Hydra's `-pe mthread`,
  `-l ioq`, and `mres` resource requests. Only the queue name is configurable.
- The tunnel user is your `$USER`.

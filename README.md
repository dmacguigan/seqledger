# seqledger

[![Lifecycle: experimental](https://img.shields.io/badge/lifecycle-experimental-orange.svg)](https://lifecycle.r-lib.org/articles/stages.html#experimental)

seqledger is a catalog of a lab's raw sequencing data (FASTQ files). It keeps one
SQLite database that records every project, sample, and file, checks that the
files are intact and backed up, and resolves sample names against NCBI and WoRMS
taxonomy. You use it from a command line (`seqledger`) or from a read-only browser
GUI. It was built for the NMNH Ocean DNA project on the Smithsonian Hydra cluster.
Paths, queue names, and similar per-site settings live in the catalog itself
([config reference](docs/reference/config.md)).

Status: prototype, in active development.

## Who this is for

| | Researchers | Data managers |
|---|---|---|
| What you do | Search and filter the catalog, look up samples and files, export tables, build copy jobs for FASTQs in Grab & Go | Everything researchers do, plus create the catalog, load new projects, run the checks, and keep it current |
| How | One command on Hydra starts the GUI; you open it in your web browser | The `seqledger` command line |
| Install | Once, into your own Hydra account | Same install |
| Read | [User guide](docs/user-guide.md) | [Admin guide](docs/admin-guide.md) |

## 60-second start: researchers

Ask your data manager for the catalog path once. Install as shown in the
[user guide](docs/user-guide.md#one-time-setup), then on Hydra:

```bash
ssh jsmith@hydra-login01.si.edu
conda activate seqledger
seqledger --db /store/nmnh_ocean_dna/public/oceandna_catalog.db gui --qsub
```

After a minute or two it prints a tunnel command. Run that command on your own
computer in a second terminal, for example:

```bash
ssh -N -L 8765:127.0.0.1:8765 -J jsmith@hydra-login01.si.edu jsmith@compute-64-12
```

Then open `http://localhost:8765` in your browser. When you are done, run the
`qdel` command it printed. Details, Windows notes, and troubleshooting are in the
[user guide](docs/user-guide.md).

## 60-second start: data managers

```bash
git clone https://github.com/dmacguigan/seqledger.git
cd seqledger
conda env create -f environment.yml
conda activate seqledger
pip install -e '.[gui]'
seqledger --version    # prints the installed version

# create the catalog and record where the data lives
seqledger --db /path/to/catalog.db init-db --name "My lab sequence catalog" \
    --seqdata-root /path/to/raw_sequence_data \
    --metadata-root /path/to/raw_sequence_metadata

# find every project folder and its <project>_mapfile.csv, load them
seqledger --db /path/to/catalog.db ingest

# check every FASTQ for damage on the I/O queue; when the jobs finish (qstat), merge
seqledger --db /path/to/catalog.db integrity --batch
seqledger --db /path/to/catalog.db integrity --collect integrity_batch/results
```

Put `export SEQLEDGER_DB=/path/to/catalog.db` in your `~/.bashrc` to drop `--db`
from every command. Read the [admin guide](docs/admin-guide.md) before the first
real ingest, and the [mapfile spec](docs/reference/mapfile.md) before writing
mapfiles.

## The `seqledger` menu

Run `seqledger` with no subcommand in a terminal for a text menu:

```
  Look things up
    1  Search samples (sample ID, species, or UniqID)
    2  Project overview
    3  Open the browse GUI

  Data manager tasks
    4  Add or update projects from the mapfiles (ingest)
    5  Check files for damage (integrity)
    6  Verify backups (make md5 listings, then load them)
    7  Look up species names (taxonomy)
    c  Use a different catalog, or create one
    q  Quit
```

Each pick prints the plain command it ran ("Same thing without the menu"), so you
can learn the commands or copy them into a script. Outside a terminal (cron,
pipes) `seqledger` with no subcommand prints usage and exits 2.

## Documentation

- [User guide](docs/user-guide.md): install, start the GUI, the views, Grab & Go,
  getting your FASTQs, troubleshooting
- [Admin guide](docs/admin-guide.md): concepts, install and update, backups,
  ingest, checks, taxonomy, hosting the GUI, routine runbook, recovery
- Reference
  - [Command line](docs/reference/cli.md): every subcommand and flag, exit codes
  - [Configuration](docs/reference/config.md): per-catalog settings
  - [Mapfile format](docs/reference/mapfile.md): columns, rules, example
  - [Status values](docs/reference/statuses.md): what each status and label means
  - [Data model](docs/reference/data-model.md): tables, columns, example SQL
- [Glossary](docs/glossary.md): Store, P-drive, Scratch, lTIO, UniqID, and more
- [CHANGELOG](CHANGELOG.md)

## Tests

```bash
conda activate seqledger
python -m pytest -q
```

Or `pip install -e '.[test]'` in any Python 3.10+ environment.

## License

MIT. See [LICENSE](LICENSE).

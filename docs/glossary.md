# Glossary

Terms used in seqledger's documentation, CLI output, and GUI.

**AphiaID**: WoRMS's numeric ID for a taxon. Stored in `taxa.aphia_id`.

**Catalog**: the seqledger database, one SQLite file ending in `.db` (for
example `oceandna_catalog.db`). Commands find it with `--db` or `SEQLEDGER_DB`.
It holds metadata and checksums only, never sequence data.

**Data-files check**: the comparison of a project's mapfile with the FASTQs on
disk. Run by `validate` and at the end of `ingest`. Shown in the GUI `data_files`
column.

**Grab & Go**: the GUI view for collecting samples and exporting them as a CSV,
a MitoPilot map, or a copy job.

**Hydra**: the Smithsonian's high-performance computing cluster. You log in to
`hydra-login01.si.edu`; jobs are submitted with SGE `qsub`.

**I/O queue, lTIO**: Hydra's queue for data-moving jobs (`lTIO.sq`). It is the
only kind of compute node that can read Store. Limits per user: 2 running jobs,
6 slots, 8 GB per slot, 72 hours. seqledger uses it for `integrity --batch`,
`gui --qsub`, and Grab & Go copy jobs. Set by the `io_queue` config.

**Integrity check**: reading every FASTQ to the end to catch truncation and
corruption (`seqledger integrity`). Different from the checksum check, which
compares Store with the backup.

**Mapfile**: the per-project CSV `<project>_mapfile.csv` that lists the
project's samples and their R1/R2 files. Lives in the metadata folder. Spec:
[mapfile reference](reference/mapfile.md). Not the same as the MitoPilot map
that Grab & Go exports.

**md5 listing**: the output of `rclone md5sum` (or `md5sum`) for one side,
Store or P-drive, loaded by `seqledger checksums`.

**MitoPilot map**: the `ID,R1,R2,Taxon` CSV that Grab & Go exports for
[MitoPilot](https://github.com/Smithsonian/MitoPilot).

**P-drive**: the NMNH network drive that holds the backup copy of Store's
sequence data. `checksums` compares it with Store. Config name of the backup
location: `backup_location`, default `pdrive`.

**Project**: one top-level folder under the sequence-data root, plus its
mapfile. The folder name is the `project_id`.

**R1, R2**: the read 1 and read 2 FASTQ files of a paired-end sample.

**Review CSV**: the file `taxonomy resolve` writes next to the catalog
(`taxonomy_review.csv` for NCBI, `worms_review.csv` for WoRMS) so you can confirm
taxon matches, then load them with `taxonomy apply`. See
[admin guide](admin-guide.md#the-review-csv).

**Sample**: one row of a mapfile: an `ID`, its R1 and R2 files, a Taxon, and a
UniqID.

**Scratch**: Hydra's fast working storage, mounted on every compute node but not
backed up. Can hold a disposable copy of the catalog for the GUI.

**seqdata root, metadata root**: the folder holding all project folders
(`raw_sequence_data`), and the folder holding all mapfiles. Config keys
`seqdata_root` and `metadata_root`.

**Store**: Hydra's large storage (`/store`), where the raw sequence data and the
master catalog live. Not mounted on ordinary compute nodes; reachable from login
nodes and the I/O queue.

**Taxdump**: NCBI's taxonomy database file (`taxdump.tar.gz`, about 72 MB).
seqledger downloads it once into `.taxonomy/` next to the catalog and indexes it
for offline name matching.

**TaxID**: NCBI's numeric ID for a taxon. Stored in `taxa.taxid`.

**Tunnel**: the `ssh -N -L` command you run on your own computer to reach the GUI
running on a Hydra node.

**UniqID**: the fifth mapfile column (also accepted as `UniqueID`): the voucher,
tissue, or specimen ID that links a sample to a physical specimen, for example
`USNM 477715`. Can repeat across projects when a specimen is re-sequenced.

**WoRMS**: the World Register of Marine Species, the second taxonomy source,
queried online.

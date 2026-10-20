# seqledger user guide

How to browse the sequence catalog in your web browser and copy the FASTQ files
you need. For people who look things up; you never change the catalog.

Contents

1. [What you need](#what-you-need)
2. [One-time setup](#one-time-setup)
3. [Start the GUI](#start-the-gui)
4. [Open the tunnel from your computer](#open-the-tunnel-from-your-computer)
5. [Stop the GUI](#stop-the-gui)
6. [The views](#the-views)
7. [What the status columns mean](#what-the-status-columns-mean)
8. [Grab & Go](#grab--go)
9. [Getting your FASTQs, start to finish](#getting-your-fastqs-start-to-finish)
10. [Quick lookups without the GUI](#quick-lookups-without-the-gui)
11. [Troubleshooting](#troubleshooting)

## What you need

- A Hydra account. You log in to `hydra-login01.si.edu` with `ssh`.
- The catalog path for your lab, from your data manager. It is a file that ends
  in `.db`, for example `/store/nmnh_ocean_dna/public/oceandna_catalog.db`.
- A terminal on your own computer: Terminal on a Mac, any terminal on Linux,
  PowerShell on Windows 10 or 11.

The GUI is read-only. Nothing you click can change or delete catalog data.

## One-time setup

You install seqledger into your own Hydra account. No admin rights needed.

1. Make sure `conda` works on Hydra. Log in and run `conda --version`. If that
   fails, set up conda first ([Hydra conda page](https://confluence.si.edu/spaces/HPC/pages/163152340/Conda+Anaconda+Miniconda), or ask your data manager).
   The GUI job runs `source ~/.bashrc` and then `conda activate seqledger`, so
   `conda activate` must work in a fresh login shell. If it does not, run
   `conda init bash` once and log in again.

2. Install seqledger:

   ```bash
   ssh jsmith@hydra-login01.si.edu
   git clone https://github.com/dmacguigan/seqledger.git
   cd seqledger
   conda env create -f environment.yml
   conda activate seqledger
   pip install -e '.[gui]'
   seqledger --version    # prints the installed version
   ```

   The `seqledger` command is available whenever the `seqledger` env is
   active; run `conda activate seqledger` in each new login.
   Keep the environment name `seqledger`. The GUI job activates the environment
   by that name (your data manager can change the name for the whole catalog,
   see [config](reference/config.md)).

3. Save the catalog path so you do not have to type it each time. Add this line
   to `~/.bashrc` on Hydra (use your lab's path):

   ```bash
   export SEQLEDGER_DB=/store/nmnh_ocean_dna/public/oceandna_catalog.db
   ```

   Log out and back in. You can still point at another catalog with `--db`.

To update later: `cd seqledger && git pull`. If the release notes say the
dependencies changed, also run `conda env update -f environment.yml`.

## Start the GUI

On Hydra:

```bash
ssh jsmith@hydra-login01.si.edu
conda activate seqledger
seqledger gui --qsub
```

(If you did not set `SEQLEDGER_DB`, use `seqledger --db /path/to/catalog.db gui --qsub`.)

`--qsub` starts the GUI as a job on Hydra's I/O queue, the only kind of node
that can read the catalog on Store. The command waits for the job to start and
prints progress lines such as `job 1234567: queued` or `running, starting
Streamlit`. This usually takes under a minute but can take longer when the queue
is busy. When the GUI is ready you see something like:

```
GUI is serving on lTIO.sq node compute-64-12:8765 with direct Store access.

On your LOCAL computer, run:

  ssh -N -L 8765:127.0.0.1:8765 -J jsmith@hydra-login01.si.edu jsmith@compute-64-12

Then open http://localhost:8765 in your browser.

Job 1234567  -  log: /home/jsmith/.seqledger_gui/gui_41234_1760876543.log
Stop the GUI when you're done:  qdel 1234567
```

The node name and port change every time. Copy them from your screen, not from
this page. You can close this Hydra window now; the GUI keeps running as a job.

Shortcut: run `seqledger` with no arguments for a menu, and pick `3`.

## Open the tunnel from your computer

The GUI only listens on the compute node itself. An SSH tunnel carries it to
your computer through the login node.

**Mac or Linux.** Open a second terminal on your own computer (not on Hydra) and
paste the `ssh -N -L` line that was printed. You are asked for your Hydra
password twice, once for the login node and once for the compute node. After
that the window seems to hang. That is correct: leave it open.

**Windows 10 or 11.** Open PowerShell and paste the same `ssh -N -L` line.
Windows includes the OpenSSH client. If `ssh` is not found, turn on "OpenSSH
Client" under Settings, Apps, Optional features.

**Old SSH clients.** `-J` needs OpenSSH 7.3 or newer. On older clients use:

```bash
ssh -N -L 8765:127.0.0.1:8765 -o ProxyCommand="ssh -W %h:%p jsmith@hydra-login01.si.edu" jsmith@compute-64-12
```

Now open the printed link, for example `http://localhost:8765`, in your web
browser.

If port 8765 is already used on your computer, change only the first number:
`ssh -N -L 8888:127.0.0.1:8765 -J jsmith@hydra-login01.si.edu jsmith@compute-64-12`,
then open `http://localhost:8888`.

## Stop the GUI

1. In the tunnel window on your computer, press Ctrl-C.
2. On Hydra, run the `qdel` command that was printed, for example `qdel 1234567`.

If you forget, the job stops by itself after 72 hours (the I/O queue limit).
Each Hydra user can run only 2 jobs at a time on the I/O queue, so a forgotten
GUI can block your next one.

## The views

Pick a view in the left sidebar. The top of every page shows which catalog file
you are reading and when it was last updated.

Every table can be sorted by clicking a column header, and has a **Download CSV**
button. Projects, Samples, and Files also have a **Filter columns** bar above the
table: one box per column, type any text to keep only matching rows.

### Projects

One row per sequencing project (one folder of FASTQ files).

- Columns: `project_id`, `source`, `description`, `n_samples`, `n_files`,
  `mapfile`, `data_files`, `checksum`, `integrity`, `owner_name`, `data_dir`,
  `date_ingested`.
- Sidebar: search by project or description, and the check boxes **Only mapfile
  issues**, **Only data-files issues**, **Only checksum issues**, **Only
  integrity issues**.
- Click a row to see, below the table, a plain explanation of any mapfile
  problem and a list of every problem file (missing, extra, damaged, or empty).
- Status cells are tinted; see [Dismissing issues](#dismissing-issues).

### Samples

One row per sample.

- Columns: `project_id`, `sample_id`, `taxon` (the name as typed in the
  mapfile), `NCBI_name` and `WoRMS_name` (links to the taxonomy pages),
  `NCBI_match` and `WoRMS_match` (how sure the name match is), `WoRMS_status`,
  `uniq_id`, `uniq_id_link` (shown when the UniqID is a web address), `flags`,
  `backup`.
- Sidebar: filter by project, by **Backup status** (this is the project's
  backup state, shown on each of its samples), and search by sample, taxon, or
  UniqID.
- The CSV download has more than the screen: full R1/R2 paths, NCBI taxid, and
  lineage.
- Click a row to list that sample's files.
- Status cells are tinted; see [Dismissing issues](#dismissing-issues).

### Files

One row per FASTQ file.

- Columns: `project_id`, `sample_id`, `direction` (R1 or R2), `filename`,
  `full_path`, `size`, `owner_name`, `backup`, `integrity`, `reads`, `checked`
  (date of the last integrity check).
- Sidebar: filter by project, backup status, integrity, or search by sample or
  filename.
- Click a row to see its sample.
- Status cells are tinted; see [Dismissing issues](#dismissing-issues).

`owner_name` is the Hydra user who owns the files on disk. It is personal
information: do not share exports that include it outside your lab.

### Taxonomy

Charts of what the catalog holds.

- **Taxonomy source**: switch everything between NCBI and WoRMS names.
- Filter by project, pick **Sunburst** or **Treemap**, the **Deepest rank** to
  show, and whether to include the `unknown` group.
- Charts: the taxonomic hierarchy (click a wedge or tile to zoom in), sample
  count by rank, resolution quality (how many names matched exactly, fuzzily, or
  not at all), and composition by project.

### Grab & Go

Build your own list of samples, then export it or copy its FASTQs. See
[Grab & Go](#grab--go) below.

## What the status columns mean

Short version. The full list, including the command-line wording, is in the
[status reference](reference/statuses.md).

**Projects: `mapfile`** (does the project folder have a usable mapfile?)

| Shows | Meaning |
|---|---|
| OK | Folder and mapfile found, every row loaded cleanly |
| flagged rows | Loaded, but some rows needed a fix (empty Taxon or UniqID set to `NA`, rows with no or duplicate ID skipped). See the sample `flags` column |
| no mapfile | Folder of FASTQs with no mapfile. Files are listed, but there is no sample information |
| no data folder | Mapfile with no folder of FASTQs |
| broken mapfile | The mapfile's header is wrong. Files are listed, no sample information |
| no usable rows | The header is fine but no row could be loaded |

**Projects: `data_files`** (do the files on disk match the mapfile?)

| Shows | Meaning |
|---|---|
| OK | Every file in the mapfile is on disk and every FASTQ on disk is in the mapfile |
| N file(s) in mapfile not on disk | Listed in the mapfile but missing from the folder |
| N file(s) on disk, not in mapfile | In the folder but not listed in the mapfile |
| unchecked | Not checked yet, or the data folder could not be reached |

**Projects: `checksum`** (is the P-drive backup an exact copy?)

| Shows | Meaning |
|---|---|
| backup verified | Every file matched its P-drive copy byte for byte |
| backup not yet verified (N file(s)) | N files have not been compared yet |
| N file(s) differ between Store and P-drive | The backup copy is different: tell your data manager |
| no files | The project has no files |

**Projects: `integrity`** (can every FASTQ be read to the end?)

| Shows | Meaning |
|---|---|
| verified | Every file was read to the end without errors |
| N corrupt | N files are damaged or truncated |
| N empty | N files are readable but hold 0 reads |
| incomplete (N unchecked) | Some files have not been checked yet |
| unchecked | No file has been checked yet |

**Files: `backup`**: Verified, Mismatch, or Not compared.
**Files: `integrity`**: `ok`, `gzip_error` (damaged or truncated), `format_error`
(line count not a multiple of 4), `unchecked` (not checked yet, or not found on
disk).
**Samples: `flags`**: `na_taxon`, `na_uniqid` (field was empty, set to `NA`),
`missing_r1`, `missing_r2` (no file name given), `r1_eq_r2` (R1 and R2 name the
same file), `extra_cells` (row had more cells than the header).
**Samples: `NCBI_match` / `WoRMS_match`**: `exact` and `confirmed` are reliable;
`fuzzy`, `near`, and `review` mean the name was a guess; `unresolved` means no
match.

### Dismissing issues

Some problems are known and harmless, for example a file you never plan to
fix. Any GUI user can dismiss an issue. Dismissing only hides it: nothing in the
catalog changes. Dismissals are shared, so everyone sees them.

Select a row, and a boxed panel appears under the table for each issue it has:

- **Projects**: a **Mapfile issue** panel with **Dismiss mapfile issue**, and
  under the **Data-files issues** table a **Dismiss known issues** panel: tick
  rows and press **Dismiss n ticked issue(s)**, or open **Dismiss all N
  issue(s)** and confirm. The table also lists md5 mismatches and each file's
  path.
- **Samples**: a **Flags for ...** panel with **Dismiss flags**.
- **Files**: a **Problems with ...** panel with one Dismiss button per problem
  (gzip error, format error, empty, md5 mismatch).

Each panel has a **Reason (optional)** box; the reason is saved with your
username and shown to everyone. "Unchecked", "Not verified", and "Not compared"
cannot be dismissed, because they are not problems yet.

After dismissing, the panel shows who dismissed the issue, when, and why, with
a **Restore** button next to it, so a mis-click is one click to undo. In the
tables, a cell whose issues are all dismissed reads `OK (+n dismissed)`; if
live problems remain it keeps their text, as in `1 corrupt (+1 dismissed)`.
Dismissed cells are sky blue. Select the row to see what was dismissed. Tick
**Show dismissed issues** in the sidebar to list dismissed rows in the Projects
issues table with who, when, and the reason, and to restore several at once
with **Restore n ticked issue(s)**.

A dismissal covers the exact state you dismissed. If the problem changes (more
flagged rows, a different error), it shows again. A dismissal stays until
someone restores it. In Grab & Go, a file whose problems are all dismissed is
copied like any other, and its sample reads `ok (+n dismissed)`.

Status cells in Projects, Samples, and Files are tinted by what they say:

| Color | Meaning |
|---|---|
| Vermillion | Error: corrupt, empty, or missing file, md5 mismatch, no or broken mapfile |
| Orange | Warning: file on disk but not in the mapfile, flagged rows, sample flags |
| Sky blue | Dismissed |
| Grey | Not yet checked: unchecked, not verified, not compared, incomplete |
| No color | OK |

Every colored cell also says what it means in words.

## Grab & Go

1. **Search.** In the sidebar, filter by project, taxonomy (matches the typed
   taxon, the NCBI name, and the NCBI lineage, so `Gadidae` finds every sample
   whose NCBI lineage includes that family), sample ID, or UniqID. Turn on
   **Regex search** for regular expressions (add a project filter on a large
   catalog; regex search looks at a limited number of rows).
2. **Add.** Select rows in "Search results" and click **Add selected**, or click
   **Add all matches**. Search again and add more; the list keeps growing.
   **Clear table** empties it.
3. **Review.** The "Grab & Go table" shows each sample with its total data size
   (`seq_data_size`), R1 and R2 read counts, and `files_ok`, which flags any
   file that is corrupt, empty, missing on disk, or not yet checked. Select rows
   and click **Remove selected** to drop them. If some files have problems, a
   warning appears with a check box (on by default) that leaves those files out
   of the copy job and the MitoPilot map. A file whose problems are all
   [dismissed](#dismissing-issues) is not a problem file: it goes into the copy
   job, the size estimate, and the MitoPilot map.
4. **Export.**
   - **Download table CSV**: one row per sample, with names, taxonomy, read
     counts, and full `r1_path` / `r2_path`.
   - **Download MitoPilot map file**: a CSV with `ID,R1,R2,Taxon` for
     [MitoPilot](https://github.com/Smithsonian/MitoPilot). Pick which field
     fills `ID` (Sample ID or UniqID). R1 and R2 are paths relative to the copy
     destination, so give MitoPilot your destination folder as `data_path`.
     Warnings appear if IDs are empty or repeated, or if a sample has several
     R1 or R2 files (only the first is used).
   - **Copy sequence data**: builds a job script that copies the FASTQs. See the
     walkthrough below.

The Grab & Go table lasts while the browser tab is open. Download the CSV if
you want to keep it.

## Getting your FASTQs, start to finish

Example: copy every Gadidae sample to your own folder on Hydra.

1. [Start the GUI](#start-the-gui) and [open the tunnel](#open-the-tunnel-from-your-computer).
2. Click **Grab & Go**. Type `Gadidae` in "Taxonomy (taxon / NCBI name /
   lineage)".
3. Click **Add all matches**.
4. Check `files_ok` in the table. Leave "Exclude problem files" ticked unless
   you have a reason not to.
5. Scroll to "Copy sequence data (Hydra I/O queue)". Check **Estimated size**
   against the free space where you are copying to.
6. In **Destination**, type a folder you can write to, for example
   `/pool/public/genomics/jsmith/gadidae_reads`. It is created if it does not
   exist. An rclone remote such as `myremote:gadidae_reads` also works.
7. Leave **Parallel transfers** (4) and **Memory per slot** (4 GB) as they are,
   unless you know you need more.
8. Click **Download submission script**. You get a file named like
   `seqledger_rclone_copy_20261019_101500.job` in your computer's downloads
   folder. (The on-screen preview shows only the first 20 files; always use the
   download.)
9. Copy the script to Hydra. From a terminal on your computer:

   ```bash
   scp ~/Downloads/seqledger_rclone_copy_20261019_101500.job jsmith@hydra-login01.si.edu:~/
   ```

10. On Hydra, submit it from your home folder:

    ```bash
    cd ~
    qsub seqledger_rclone_copy_20261019_101500.job
    qstat
    ```

    `qstat` shows the job as `qw` (waiting) and then `r` (running). When it is
    gone from the list, it has finished.
11. Check the log, written to the folder you ran `qsub` from:

    ```bash
    tail seqledger_rclone_copy_20261019_101500.log
    grep ERROR seqledger_rclone_copy_20261019_101500.log
    ```

    The last line starts with `=` and says `done`. Any `ERROR` line means a copy
    failed.
12. Your files are under the destination with the same layout as on Store:
    `/pool/public/genomics/jsmith/gadidae_reads/<project>/<subfolders>/<file>.fastq.gz`.

If the job stops at the 72-hour limit, submit the same script again. It skips
files that were already copied.

Only need a handful of files? Download the table CSV and copy the `r1_path` and
`r2_path` files with `cp` on a Hydra login node.

## Quick lookups without the GUI

On Hydra, with the environment active:

```bash
seqledger query search Urophycis            # samples whose ID, taxon, or UniqID contains the text
seqledger query uniq-id "USNM 477715"       # samples with exactly this UniqID
seqledger query summary                     # one row per project
```

Output is tab-separated. `seqledger query -h` lists every lookup. Or run
`seqledger` alone for the menu.

## Troubleshooting

| You see | What to do |
|---|---|
| `no catalog at 'catalog.db'` | The path is wrong or `SEQLEDGER_DB` is not set. Check the path with your data manager, then pass `--db /path/to/catalog.db` or fix `~/.bashrc`. |
| Stuck at `queued (lTIO busy -- max 2 jobs/user)` | You already have 2 jobs on the I/O queue (an old GUI, a copy job). Run `qstat`, then `qdel` the ones you do not need. |
| `job <id> is no longer queued/running -- it may have failed` | Open the log path it printed. A `conda: command not found` or activate error means conda is not set up in `~/.bashrc` (run `conda init bash`). If the job dies right away, it may need more memory: `seqledger gui --qsub --mem 10`. |
| `did not report ready within 300s` | The queue was slow. Run `qstat` to see if it is still waiting; you can retry with `--wait 600`. Remember to `qdel` the old job. |
| `qsub not found` | You are not on a Hydra login node. Log in to `hydra-login01.si.edu` first. |
| Browser says "can't connect" or "connection refused" | The tunnel window is closed or shows an error, or the port in the link does not match the tunnel. Paste the printed tunnel command again. |
| Tunnel says `Address already in use` | Another program uses that port on your computer. Change the first number, see [the tunnel section](#open-the-tunnel-from-your-computer). |
| `Catalog is being updated, retry shortly.` | Your data manager is writing to the catalog. Reload the page in a few seconds. |
| `Catalog database not found` | The GUI was started with a wrong path. `qdel` the job and start again with the right `--db`. |
| `This catalog copy is older than this GUI` | Ask your data manager to update the catalog. |
| The "last updated" date at the top is old | You may be reading a copy of the catalog. Ask your data manager. |
| A message says "Run `seqledger taxonomy resolve`" or "Run `validate --seqdata-root`" | That step has not been run yet. Ask your data manager. |
| Taxonomy view: "needs plotly" | Run `conda activate seqledger && conda env update -f environment.yml` in your `seqledger` folder. |
| The GUI stopped by itself | Jobs on the I/O queue end after 72 hours. Start it again. |

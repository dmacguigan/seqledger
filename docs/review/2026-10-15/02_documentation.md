# seqledger documentation review (branch devel, cbd7759)

Scope: README.md, CHANGELOG.md, CONTRIBUTORS.md, CLAUDE.md, pyproject.toml, all argparse
help in seqledger/cli.py (captured output: scratchpad/docs/help.txt), module/function
docstrings in seqledger/*.py, seqledger/schema.sql comments, and all user-facing text in
seqledger/app/streamlit_app.py. Nothing in the repo was modified.

## (a) Verdict

1. The README is detailed and mostly honest, but it is one 500-line page that serves two audiences, has no TOC, and puts the core CLI reference inside a single 130-line bash comment block (README 204-332).
2. The README disagrees with the code in about 25 places. The most serious: `taxonomy apply` confirms every row, not only the rows you edited; `init-db --show` is not read-only; the `backup_location` config key does nothing; root-path precedence is described wrongly; and the mapfile column order is not documented.
3. The GUI-user path (README 25-88) works on its own for launching. But the guide to what the GUI's columns and statuses mean sits in the data-manager section (README 370-408), and some GUI empty states tell browse-only users to run CLI commands.
4. A first-time data manager has no mapfile spec with an example, no directory-layout diagram, no status reference, no backup/restore or update procedure, no Scratch-copy procedure, and no list of where logs go.
5. Housekeeping is thin. pyproject says 0.1.0, but the CHANGELOG has only [Unreleased], and there are no tags and no `--version`. There is no CITATION.cff, no CONTRIBUTING.md, and no issue templates. Non-ASCII characters (em dashes, arrows, emoji) appear on 48 lines across docs and code strings.

## (b) Accuracy mismatches

"README" = README.md line. Code refs are file:line on devel.

| README line | says | code line | actually |
|---|---|---|---|
| 39, 163 | `git clone https://github.com/OWNER/seqledger.git` | `git remote -v` | Real URL is `https://github.com/dmacguigan/seqledger.git`. Unfilled placeholder in two copy-paste blocks. |
| 425 | default `Ocean DNA ...` (literal ellipsis char) | db.py:26 | `Ocean DNA sequence data catalog`. Also note 21, 61, 185 put a Unicode ellipsis inside command examples (`seqledger ... gui --qsub`, `seqledger --db ... init-db`), which breaks copy-paste. |
| 431 | `backup_location` controls the "verified backup" location label, default `pdrive` | db.py:34; checksums.py:169; query.py:35; streamlit_app.py:281 | Key is stored but never read. `'pdrive'` is hard-coded in all three consumers. Setting it does nothing. Same for `init-db --backup-location` (cli.py:693). |
| 418 | `init-db --show   # print resolved config` | cli.py:699, 75-104 | Help says "print the resolved config and exit". Code first runs `init_db` (creates the file if missing, runs irreversible migrations including the files-table rebuild), applies any `--set`, then prints. Not read-only. |
| 97-98 | "migrations add columns in place and are not reversible" | db.py:183-215, 241-245 | Migrations also drop and rebuild the `files` table (rel_path re-key). They can raise `RuntimeError` on duplicate rel_paths (db.py:198). README should say so; CHANGELOG does. |
| 434-437 | required columns are "a fixed set ... header names are matched case-insensitively" | db.py:271-278; validate.py:124 | Also positional: the first five columns must be, in order, `ID,R1,R2,Taxon,UniqID` (or `UniqueID`). A file with the right names in another order is `broken_mapfile`. Whitespace-tolerant too. |
| 213-217 | lists metadata_status `missing_mapfile`, `missing_seqdata`, `broken_mapfile`, `ok` | ingest.py:9-17, 99, 391 | Six values. Missing `flagged` (rows NA-filled/skipped) and `invalid_mapfile` (valid header, zero loadable rows). The README's own troubleshooting table (445-446) uses their GUI labels, so the page contradicts itself. |
| 132-133 | "now: required non-null fields, `R1 != R2`, unique sample IDs per project" | validate.py:53-97 | Not enforced as rejections any more. Empty Taxon/UniqID become `NA`; R1==R2 is loaded once and flagged; empty or duplicate IDs are skipped with a WARN. Only a bad header or filename suffix FAILs. |
| 236-239 | manual map file is "two-column map file (metadata csv, data dir), same format as scripts/.../example_map_file.txt" | ingest.py:176-192 | Path does not exist in the repo. Actual format: first line ignored as header; each later line split on whitespace (not commas); first two tokens used. Not stated anywhere. |
| 239 | "Per-project CSVs are looked up in --metadata-root (default: the map file's own dir)" | cli.py:175; ingest.py:670-675 | Order is: CSV path as given (absolute, or relative to the current directory) first; then `--metadata-root`; then the configured `metadata_root`; and only if none is set, the map file's dir. |
| 251-252 | data-files check "needs --seqdata-root to scan disk" | validate.py:281-287; cli.py:365 | Falls back to the configured `seqdata_root`, then to each project's stored `projects.seqdata_root`. It scans disk without the flag. The CLI still prints "(data-files check skipped: pass --seqdata-root to scan disk)" (cli.py:371-372) even when it did scan. Wrong message. |
| 267 | integrity: "--seqdata-root is optional; each project's ingest-time root is used by default" | cli.py:529; integrity.py:135 | If config `seqdata_root` is set, it overrides every project's stored root. A lab with projects under two roots gets them marked `unchecked` (file not found). Same precedence bug in validate (cli.py:365). Same misleading help at cli.py:743. |
| 274-275 | batch job env is `source ~/.bashrc; conda activate seqledger` | cli.py:428, 546 | Activates the configured `conda_env`. cli.py:501 also hard-codes "submitted N job(s) to lTIO.sq" regardless of `io_queue`. |
| 120 | "Integrity, checksums, taxonomy, and query are separate steps" | cli.py:262-285 | Ingest auto-runs NCBI taxonomy for new taxa (as README 219-220 itself says). Only WoRMS is separate. |
| 226-228 | `--prune-projects` "refuses to run if the roots turn up no projects" | ingest.py:636-656; cli.py:253-255 | Also refuses if either root is missing, and if the prune would remove more than 50% of that root's projects. There is no CLI flag to override (`force=True` is API-only). The CLI ignores `pr["reason"]` and always prints "no projects discovered under the roots", which is wrong for the 50% case. |
| (absent) | `--prune` / `--prune-projects` usage | cli.py:142-149, 725 | Non-interactive (cron, pipe) prune is refused unless `--yes` is given. README never mentions `--yes`, so a cron job written from the README fails. |
| 299 | "Downloads a pinned NCBI taxdump" | taxonomy.py:20, 110-126 | Downloads the current `taxdump.tar.gz` (about 72 MB) once and records the date in `.taxonomy/TAXDUMP_VERSION.txt`. Frozen locally, not pinned to a release. |
| 309-311 | "edit confirmed_taxid in taxonomy_review.csv for any wrong fuzzy/unresolved rows, then fold the overrides back in" | taxonomy.py:616, 641-659; worms.py:359 | The review CSV pre-fills `confirmed_taxid` with the auto-matched taxid on every row. `apply` marks every non-blank row `confirmed`, including fuzzy guesses nobody checked. You must blank a cell to leave that row unconfirmed. Same for `confirmed_aphia_id`. High impact on data quality. |
| 309-311 | `--review taxonomy_review.csv` (bare relative path) | cli.py:278, 595 | The CSV is written to `<db dir>/taxonomy_review.csv`, not the current directory. It is overwritten by every `taxonomy resolve` and by any `ingest` that finds new taxa (then holding only the new taxa), which loses in-progress edits. |
| 336-337 | backfill: run `checksums` "per project (`--project ...`) in the background so you don't monopolize Store read bandwidth" | checksums.py:58-151 | `checksums` only parses two text files. The Store-heavy step is `rclone md5sum`; scope that by path instead. `--project` only filters what is loaded. |
| 244-247 | `rclone md5sum <root> > store.md5` | checksums.py:34-35, 54-55, 100 | Not stated: both listings must be rooted exactly at the raw_sequence_data level, so each path starts with the project folder, or nothing matches. `SI-Hydra:` is an rclone remote that must already be configured. |
| 460-461 | "`validation_log` records validation runs" | integrity.py:149-151 | Only `integrity` (local or `--collect`) writes it. `validate` never does. `report_relpath`, `projects.notes`, and `backups.notes` are never written. |
| 375, 447 | data_files shows `"N missing, M orphan"` / `"N missing / M orphan"` | streamlit_app.py:397-400 | GUI shows `N missing from disk, M missing from mapfile`. The CLI (cli.py:345-348) uses "missing/orphan". README matches the CLI, not the GUI. |
| 372-382 | Projects view check fields: mapfile, data_files, checksum. Files view: path, size, owner, backup | streamlit_app.py:663-666, 649, 723, 739-741 | Projects also has an `integrity` column and an "Only integrity issues" filter. Files has `integrity`, `reads`, `checked` columns and an Integrity filter. Not documented. |
| 405-406 | "a clickable `UniqID link` column" | streamlit_app.py:236 | Header is `uniq_id_link`, cell text `open` plus a non-ASCII arrow. |
| 399 | copy job runs `module load tools/rclone/1.66.0` | streamlit_app.py:1285 | Uses the configured `rclone_module` (default is that value). Fine as an example, but should say "configurable". |
| 7-8, 412-415 | configurable to "retarget another lab's ... cluster settings" | cli.py:417-419; gui.py:102-103; rclone.py:117-119 | Scheduler is hard-wired SGE `qsub` with Hydra-specific `-pe mthread`, `-l ioq`, and `mres`. Only the queue name is configurable. Add these to the "Not (yet) configurable" note. |
| 414-415 | "Because the GUI reads a copy of the DB, config travels with it" | gui.py:5-9 | The recommended `--qsub` mode reads the master, not a copy. Reword: "config is stored in the .db, so any copy carries it". |
| 451 | "a CLI command prints a one-line `error:`" | cli.py:834; taxonomy.py:104; db.py:198; cli.py:273 | `RuntimeError` is not caught. A first `ingest` on an offline node (taxdump download fails) or a migration duplicate prints a full traceback. On ingest that happens after the metadata has been committed. Code fix, but the docs promise otherwise. |
| 195 | `mitopilot.py  [MitoPilot](https://...) map-file export` | n/a | Markdown link inside a fenced block renders literally. Layout list also omits `worms.py`, `__main__.py`, `environment.yml`, and `requirements.txt`. |
| docstring | streamlit_app.py:3 "Launch via `seqledger gui --db PATH`" | cli.py:672 | `--db` is a global option. `seqledger gui --db PATH` is an argparse error. Must be `seqledger --db PATH gui`. |
| docstring | rclone.py:3 "The GUI \"Custom table\" view" | streamlit_app.py:1314 | View is named "Grab & Go". |
| comment | schema.sql:90 "Populated by `seqledger.py taxonomy resolve`" | pyproject.toml:98 | No `seqledger.py`. Command is `seqledger taxonomy resolve`. |
| comment | schema.sql:36, 150 "on-disk fastq.gz"; `data_check_issues.filename` | validate.py:193-199; streamlit_app.py:357 | Extensions are configurable; the `filename` column now stores rel_path, not a basename. |
| docstring | cli.py:10 "taxonomy  resolve sample taxa to NCBI TaxIDs" | cli.py:786 | Omits WoRMS. |
| CHANGELOG 44 | "CSV-injection sanitization on exported map files and tables" | streamlit_app.py:1233-1235 | The MitoPilot map-file download is not passed through `_csv_safe_df`. Either sanitize it or narrow the claim. |

## (c) Structure

Problems:

- No TOC. 15 H2 sections in an odd order: Who / GUI users / Requirements / Quick start / **Why** / **Design** / Install / Layout / Usage / Backfill / GUI / Configuration / Troubleshooting / Schema / Open items / Tests. Background ("Why", "Design at a glance") sits between Quick start and Install.
- Usage (204-332) is one bash block with all the prose in `#` comments. You cannot link to `integrity --batch`, search results land mid-block, and it does not render as prose. Each command needs its own heading.
- The two audiences are mixed up. The GUI feature guide (370-408) is under the data-manager "GUI (over SSH tunnel)" heading. The GUI-user section's view list (76-81) is a stub. Env setup appears three times (37-44, 162-168, 343-348).
- Configuration (410-438) is findable but incomplete. It has no "CLI flag" column (`--slug` maps to `catalog_slug`, `--fastq-ext` to `fastq_extensions`), no note that `--set` accepts unknown keys silently, and no precedence rule (flag > config > built-in default, plus the per-project root caveat above).
- "Open items" (486-492) holds internal TODOs. Move them to GitHub issues.
- Terminology is overloaded. "map file", "mapfile", and "map-file" all appear, and the word names three different things: the per-project `<project>_mapfile.csv`, the manual two-column map file, and the MitoPilot export. Also "Store", "/store", and "NAS"; "P-drive" and "pdrive"; "verified" for both checksum and integrity.

Recommendation: yes, split into `docs/`. Plain Markdown on GitHub is enough; no site generator is needed yet.

```
README.md                 (~120 lines) what it is, the two audiences, 60-second start for each,
                          links to docs/, license, citation
docs/
  user-guide.md           GUI users only
    1 One-time setup (conda on Hydra, conda init in ~/.bashrc, install)
    2 Start the GUI (gui --qsub), the tunnel on Mac/Linux/Windows, stop it
    3 The views: Projects, Samples, Files, Taxonomy, Grab & Go
    4 What the status columns mean (links to reference/statuses.md)
    5 Grab & Go: build a set, CSV, MitoPilot map file, rclone copy job
    6 Troubleshooting (tunnel, port in use, "catalog is being updated", stale banner)
  admin-guide.md          data managers
    1 Concepts + directory layout diagram
    2 Install, update, back up and restore the catalog
    3 First catalog: init-db + config
    4 Mapfiles (link to reference/mapfile.md)
    5 Ingest (auto-discovery, manual map file, prune, --yes)
    6 Validate (data-files) and checksums (rclone md5sum on both sides)
    7 Integrity: local, --batch, --collect, resume
    8 Taxonomy: NCBI and WoRMS resolve, review CSV, apply
    9 Hosting the GUI: --qsub vs Scratch copy (how to sync safely)
   10 Routine runbook + cron example
   11 Logs, troubleshooting, recovery
  reference/
    cli.md                generated from --help (one section per subcommand)
    config.md             key | flag | default | used by | notes
    mapfile.md            full spec + example
    statuses.md           every status value, who sets it, GUI label
    data-model.md         tables/columns + example sqlite3 queries
  glossary.md             Store, P-drive, Scratch, lTIO, mapfile, map file, project, sample, UniqID
CHANGELOG.md  CONTRIBUTING.md  CITATION.cff  LICENSE
```

## (d) Onboarding gaps, ranked

1. **Mapfile spec with a full example (missing).** Needs: name `<project>_mapfile.csv`, where the `<project>` part must exactly equal the top-level folder name; location `metadata_root`; first 5 columns positional (`ID,R1,R2,Taxon,UniqID`); R1/R2 are basenames, not paths, matched anywhere under the project folder (recursive); extra columns kept in `samples.extra_json`; empty Taxon/UniqID become `NA`; empty or duplicate IDs are skipped; UTF-8 or cp1252 (Excel) accepted; the flag tokens. Also the manual two-column map file format (whitespace-separated, header skipped). Suggested example:
   ```
   ID,R1,R2,Taxon,UniqID,Collector
   S01,S01_R1.fastq.gz,S01_R2.fastq.gz,Gadus morhua,USNM 477715,Smith
   ```
2. **Directory layout diagram (missing).** `seqdata_root/<project>/[any/subdirs/]*.fastq.gz`, `metadata_root/<project>_mapfile.csv`, catalog dir (`catalog.db`, `.taxonomy/`, `taxonomy_review.csv`, `worms_review.csv`), `./integrity_batch/{scripts,logs,results}`, `~/.seqledger_gui/`. Also the project-folder naming convention `genohub-<num>_<desc>` / `LAB-<run>_<desc>`, which fills `source` / `project_number` / `description` (db.py:18-19, 281-296). Undocumented, but it drives the GUI `source` column.
3. **Status reference / state machine (scattered or missing).** See the table below. Two easy-to-miss traps: `integrity_status` NULL (never checked) and `'unchecked'` (file missing, or no root) both display as "unchecked"; and a file can be "ok" with 0 reads (GUI flags it as "empty").
4. **Taxonomy review semantics.** Prefilled confirmed_* columns, CSV location, overwrite on re-resolve or ingest, network needs (NCBI about 72 MB once; WoRMS online every run, cached), and that compute nodes may be offline.
5. **Checksums procedure.** Where to run each `rclone md5sum` (Store from Hydra; the P-drive listing in the example is from a Mac mount), rclone remote setup, the root must be the raw_sequence_data level, the meaning of `--source ingest|backfill` (a label only), and the "listing much smaller than catalog" truncation warning.
6. **Backup and restore of the catalog.** "Back up first" appears 3 times with no how-to. Give `sqlite3 catalog.db ".backup catalog_YYYYMMDD.db"` (safe while readers are open; `cp` is safe only when no writer is running), where to keep copies, and how to restore (replace the file, then re-run `init-db` only if the code is newer).
7. **GUI Scratch-copy workflow.** README 143-144 and 362-367 mention "a read-only copy synced to Scratch" but never how: the sync command, how often, Scratch purge policy, and that the GUI banner shows the copy's mtime (streamlit_app.py:1294-1298).
8. **Updating the tool.** `git pull` (editable install, so no reinstall unless deps change), `conda env update -f environment.yml` if deps changed, back up, `init-db` to migrate, and read the CHANGELOG first. Missing entirely.
9. **Logs (no single place lists them).** Integrity batch: `<batch-dir>/logs/integrity_<project>.log`. GUI job: `~/.seqledger_gui/gui_<stamp>.log` (and `.job`, `.ready`; never cleaned up). rclone copy job: `seqledger_rclone_copy.log` in the submit dir. CLI: stdout only (suggest `2>&1 | tee`). Integrity run history: the `validation_log` table.
10. **Concurrency rule.** One writer at a time: do not run two ingest/validate/checksums/integrity/taxonomy commands on the same .db at once. The GUI and batch workers are read-only. Implied in the code comments, never stated for users.
11. **Prune safety details.** `--yes` for cron, the 50% blast-radius cap with no CLI override, and that the manual map-file mode ignores `--prune-projects`.
12. **Recovery recipes.** Migration duplicate (db.py:198), offline taxdump, `qsub not found`, `conda activate` failing inside a job (conda not initialised in `~/.bashrc`; on Hydra a module may be needed), GUI job stuck in `qw` (2-job lTIO cap shared with integrity batch jobs).
13. **Routine runbook.** For example, weekly: ingest, then checksums after the backup, then integrity `--batch --only-unchecked`, then `--collect`, then refresh the GUI copy. Plus a cron example with `--yes` and `SEQLEDGER_DB`.
14. **Permissions and privacy.** Who needs write access to the db dir (all writers; review CSVs land there), GUI readers need read access, and `owner_name` is personal data (the GUI says so; the README does not).
15. **Optional speedup.** `python-isal` is used automatically if installed (integrity.py:30-40). Not mentioned.

Status reference (proposed content for docs/reference/statuses.md):

| field | values | set by | GUI shows |
|---|---|---|---|
| projects.metadata_status | ok, flagged, missing_mapfile, missing_seqdata, broken_mapfile, invalid_mapfile (GUI adds unknown for NULL) | ingest (auto-discovery) | OK, flagged rows, no mapfile, no data folder, broken mapfile, no usable rows |
| projects.data_check_status | unchecked, ok, issues | validate; ingest post-check; reset to unchecked by --prune without a root | OK, "N missing from disk, M missing from mapfile", unchecked |
| files.md5_match | 1, 0, NULL | checksums (NULL if absent from both listings) | Files: Verified / Mismatch / Not compared. Projects checksum: verified, N mismatch, incomplete (N uncompared), no files |
| backups.verified (location 'pdrive') | 1, 0 | checksums | Samples "backup": Verified / Not verified (project-level, shown per sample) |
| files.integrity_status | NULL, ok, gzip_error, format_error, unchecked | integrity / --collect | Files: raw codes. Projects: verified, N corrupt, M empty, incomplete (N unchecked), unchecked, no files |
| taxa.match_type | exact, fuzzy_species, fuzzy_genus, fuzzy_higher, unresolved, confirmed | taxonomy resolve / apply | NCBI_match |
| taxa.worms_match_type | exact, phonetic, near_1..3, match_quarantine, unresolved, confirmed | resolve/apply --source worms | WoRMS_match |
| validation_log.status | pass, warn, fail | integrity only | not shown |

## (e) CLI help and GUI text nits

CLI help (cli.py):

- No subparser has `description=`, so `seqledger init-db --help` etc. print no summary line. Add descriptions and an `epilog` with one example each.
- No `--version` (`python -m seqledger --version` gives "the following arguments are required: cmd"). Add it, reading `importlib.metadata.version`.
- 733: `checksums --source {ingest,backfill}` has no help. It is only a label stored in `files.md5_source`; say so and give the default (backfill).
- 730-731: `--store` / `--pdrive` say "rclone md5sum output for Store". Add "paths relative to the raw_sequence_data root".
- 780-783: `query` help is just "lookups". `what` and `term` have no help. Say which choices need `term` (uniq-id, search) and what each prints. `query uniq-id` with no term silently prints "(none)".
- 699: `--show` "print the resolved config and exit". Not true (see table). Either make it read-only or say "also creates/migrates the schema".
- 718-719: `--prune` "(then re-run `validate --seqdata-root`)" is stale; ingest now refreshes the data-files check itself (cli.py:287-317).
- 713-714: `ingest --seqdata-root` "Required for auto-discovery". Not if the config key is set. None of the root flags (ingest 709-714, validate 737, integrity 742) mention the `seqdata_root` / `metadata_root` config fallback.
- 738: validate `--seqdata-root` "enables the on-disk data-files check". Misleading; it runs anyway via config or stored roots.
- 743: integrity `--seqdata-root` "(default: each project's stored root)". Config wins when set.
- 752-755, 815, 819: help hard-codes "lTIO.sq"; better "the configured io_queue (default lTIO.sq)".
- 681-696: init-db flags give no defaults (`--conda-env` default seqledger, etc.), and flag names differ from the keys (`--name` vs catalog_name, `--slug` vs catalog_slug, `--fastq-ext` vs fastq_extensions). Show the key: "sets config key catalog_slug".
- 693: `--backup-location` documents a no-op.
- 794: `--force-download` "re-download the taxdump". It also rebuilds the index; worth saying.
- 788-789: `taxonomy resolve` does not say where the review CSV goes or that it is overwritten.
- 802: `apply --review` "edited review CSV". Add the default location and "every non-blank confirmed_* row is applied".
- 810-811: gui `--port` gives no default in the text (8501), and local mode may silently pick another port (gui.py:55).
- Tone: mostly lowercase noun-phrase fragments, which is fine. Outliers are "lookups" (bare noun), the use of "+" for "and" ("load + compare", "gzip + FASTQ"), and parenthetical asides of mixed length. `--only-unchecked` (767-771) is 4 lines; trim.
- Runtime messages: cli.py:253-255 ignores the real refusal reason; cli.py:371-372 claims a skip that did not happen; cli.py:501 hard-codes lTIO.sq.

GUI text (streamlit_app.py):

- Empty states tell browse-only users to run admin commands. 713-714 "Run `validate --seqdata-root` and `integrity --collect` to refresh." 878 "Run `seqledger taxonomy resolve`." Suggest "Not checked yet. Ask your data manager to run ..."
- 674-677 Projects caption: "'mapfile' flags a folder with no mapfile, a mapfile with no folder, or a broken mapfile". Omits "flagged rows" and "no usable rows".
- 700 subheader "Data-files issues: <pid>" also lists integrity failures (gzip error, format error, empty). Rename to "File issues".
- 1123 caption "click **Add to custom table**". The button is "Add selected (N)" (1133) and the table is the "Grab & Go table" (1149).
- "verified" is overloaded. Projects `checksum` = verified (backup md5 match) and Projects `integrity` = verified (gzip OK), while Files `backup` = "Verified" with a capital V. Use "backed up" / "intact" or similar distinct words.
- Mixed vocabulary for "not yet checked": "Not compared" (Files backup), "incomplete (N uncompared)" (checksum), "unchecked" (integrity, data_files).
- 722-724 Files Integrity filter uses raw codes (`gzip_error`, `format_error`), while the Projects column says "corrupt" / "empty". Pick one set.
- 573 Samples "Backup status" filters on the project-level `backups.verified`, but it reads as per-sample. Label it "Project backup".
- Column headers mix styles: snake_case (`project_id`, `owner_name`, `data_dir`, `seq_data_size`), mixed case (`NCBI_name`, `WoRMS_match`), and plain words (`reads`, `checked`, `issue`, `detail`). Also `full_path` becomes `expected_path` in the issues table (709), `data_dir` actually holds a relative path, and `seq_data_size` (display) vs `seq_data_size_gb` (export).
- 128 issue detail says "`gunzip -t`"; README and integrity.py say `gzip -t`.
- 1229 "MitoPilot ID column" choices are "Sample ID" / "UniqID", while the README uses "sample ID", "ID", and "UniqID/UniqueID". Fine, but put them in a glossary.
- 1304-1308 fallback title "Sequence data catalog" while the CLI default is "Ocean DNA sequence data catalog".

Docstrings and comments:

- "Ocean DNA" branding remains in `__init__.py:1`, `cli.py:2`, `db.py:1`, `taxonomy.py:1`, `streamlit_app.py:1`, and `schema.sql:1`, while the tool now presents itself as generic.
- `ingest.py:503` says "every *.fastq.gz"; it uses the configured extensions. Note also that only gzip input is supported: an uncompressed `fastq` extension would fail integrity as `gzip_error`, and `_DIRECTION_RE` (ingest.py:129-130) only infers R1/R2 for `.fastq.gz` / `.fq.gz`. Say so in config.md.

Non-ASCII (project rule: ASCII in code; README requested ASCII and no em dashes):

- Code strings: gui.py:200 (middle dot); streamlit_app.py:236 (arrow), 556 (magnifier emoji), 904 and 1004 (single angle quote), 998 (up arrow), 1001 (house), 1004, 1012, 1100, 1104, 1249, 1298, 1331 (em dash), 1133 and 1139 (heavy plus), 1144 (wastebasket), 1195 (heavy minus), 1245 (ellipsis). 19 lines in total.
- README.md: 24 em dashes on lines 12, 18 (x2), 22 (x2), 29, 35, 51, 64, 65, 78, 79, 80, 83, 87, 92, 97, 120, 426, 434, 444, 448, 449, 450. Right arrows on 23 (x2). Ellipses on 21, 61, 185, 425 (three of them inside commands).
- CONTRIBUTORS.md: em dash on lines 3 and 4.
- CHANGELOG.md, CLAUDE.md, pyproject.toml, schema.sql: clean.

Housekeeping:

- Version: pyproject 0.1.0, but the CHANGELOG has no `[0.1.0]` section, no tags, no `__version__`, and no `--version`. Either cut 0.1.0 (move Unreleased to `## [0.1.0] - date`, tag it) or call the current state 0.1.0.dev0.
- CHANGELOG: missing `[Unreleased]: compare/...` link refs; "driven by an adversarial review" and "specifics may still shift" are internal notes; "Fixed" bullets are too vague for users ("Checksum and integrity correctness fixes"). Missing user-visible items: the new `--yes` flag and the non-interactive prune refusal (a behaviour change for cron), `SEQLEDGER_DB` support, CHECK constraints for new catalogs only, deleted files now surfacing as `unchecked`, checksums resetting `verified` when files vanish, the GUI staleness banner, and the GUI integrity column. No issue/PR links (commit 990f9a1 cites issue #4).
- License: LICENSE (MIT) and pyproject agree. The README never mentions the license. `license = { text = "MIT" }` is the legacy form; PEP 639 is `license = "MIT"` (needs setuptools>=77). No `[project.urls]` (Homepage, Issues, Changelog), no classifiers.
- Citation: none. Add CITATION.cff (and a README "Citing" line) if this will be cited in NMNH work.
- Contributing: CLAUDE.md holds the real workflow (devel branch, migrations must be idempotent, CHANGELOG rules), but it is written for the agent. Add a short human CONTRIBUTING.md covering the same rules, plus `pip install -e '.[test]'` (the `test` extra is never mentioned in the README; README 494-498 omits the env needed).
- No `.github/` (no issue/PR templates, no CI). A bug template asking for `seqledger --version`, the command, and `--debug` output would help.
- pyproject `authors` lists "Claude (Anthropic)" with `noreply@anthropic.com`, which will show in package metadata. Confirm this is intended.

## (f) Top 5 fixes, ranked by reader impact

1. **Correct the taxonomy review instructions** (README 309-311, 322-323, `apply --review` help). Say that the CSV is pre-filled, every non-blank `confirmed_taxid` / `confirmed_aphia_id` is marked confirmed, you blank rows you have not checked, where the file lives (`<db dir>/`), and that resolve/ingest overwrite it. Otherwise unchecked fuzzy matches get silently promoted to "confirmed".
2. **Write the mapfile spec and the directory-layout diagram** (new reference/mapfile.md, linked from Quick start). Include column order, basenames, NA handling, naming that must match the folder, the manual map-file format, and the project-folder naming convention. Remove the dead `scripts/.../example_map_file.txt` reference. This is the first thing a new data manager gets wrong.
3. **Fix the root-path and `--show` behaviour, or document it exactly** (README 251-252, 267, 418; cli.py:365, 529, 699, 738, 743, 371-372). Today a configured `seqdata_root` silently overrides per-project roots, a "skipped" message prints when the check ran, and a "show" flag migrates the database. Pick a precedence (flag > per-project stored root > config, or the reverse), state it once in config.md, and make the help match.
4. **Split the README into a short landing page plus docs/user-guide.md and docs/admin-guide.md**, using the outline in (c). Move the GUI feature guide (370-408) and a column/status glossary into the user guide. Replace the 130-line bash comment block with one heading per command. Add a TOC. Fix the `OWNER` placeholder and the ellipses inside commands.
5. **Add an operations page**: back up and restore the .db (`sqlite3 .backup`), update the tool (pull, back up, `init-db`), sync the Scratch copy safely, list every log location, the one-writer rule, `--yes` for cron, and the status reference table from (d). Also delete the dead `backup_location` key, or wire it up.

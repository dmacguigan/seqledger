# seqledger browse GUI: UI/UX review (devel @ cbd7759)

Scope: seqledger/app/streamlit_app.py (1336 lines), seqledger/gui.py, README GUI sections, tests/test_gui_app.py.
Method: fake catalog built with the real CLI (init-db, ingest, integrity, validate, checksums; taxa rows inserted by hand),
5 projects / 42 samples / 96 files covering every status (ok, flagged rows, no mapfile, no data folder, missing from disk,
orphan, gzip error, format error, empty 0-read file, md5 mismatch, unchecked). Every view driven with
streamlit.testing.v1.AppTest (streamlit 1.59.0, pandas 3.0.3, plotly 7.1.0 installed into scratch). Also tested: empty catalog, catalog missing
`data_check_issues`, catalog with pre-integrity `files` table, missing DB path, exclusively locked DB, and a synthetic
5,000-sample / 50,000-file / 3,000-taxon catalog. `streamlit run ... --server.headless true` boots and
`/_stcore/health` returns 200 ok. Scratch scripts and DBs: /tmp/claude-1000/-home-dmacguig-Documents-GitHub-seqledger/3bcc3cd7-35f2-45f8-8e0b-6b4fa8b86b3c/scratchpad/ux/

## (a) Verdict

1. Solid, honest data tool: read-only, fast (every view under 0.35 s server-side at 50k files), text statuses, good link columns.
2. One real crash class: row selections are positional and survive filtering, so filtering after a click gives a traceback or the wrong project/sample.
3. Grab & Go, the main task for biologists, has correctness gaps: MitoPilot map breaks for multi-project or lane-split carts, bad files flow silently into copy jobs, and a 5k-sample cart renders a 2.5 MB script.
4. Vocabulary drifts between views and the CLI ("backup" means 3 things, "orphan" vs "missing from mapfile", "corrupt" vs gzip_error, empty files are "ok" in Files), and browse-only users are told to run data-manager commands.
5. Old catalog copies crash with raw tracebacks on the default view; a locked DB gives about 63 s of blank page.

## (b) Findings, ranked by user impact

### 1. HIGH. All table views. Positional row selection breaks after filtering (traceback or wrong record)
- Problem: detail panels and cart buttons read `show.iloc[sel[0]]` / `view.iloc[i]`. Streamlit keeps a keyed dataframe's selection when its data changes, so after a user selects a row and then filters, the stored row number points past the end (crash) or at a different record.
- Evidence: streamlit_app.py:625, 685, 756, 1135, 1197. Streamlit's own comment at site-packages/streamlit/elements/arrow.py:1047 ("selections can become orphaned when the data changes - e.g. when rows get removed"); identity is key plus selection_mode only (arrow.py:1051). AppTest: select Projects row 4, tick "Only integrity issues" -> `IndexError: single positional indexer is out-of-bounds` at streamlit_app.py:685. Select row 1 (Belize), tick "Only integrity issues" -> panel shows "Data-files issues: genohub-2002_Hawaii" (wrong project, no error).
- Same risk in Grab & Go: "Remove selected" after the cart changes can remove the wrong samples.
- Fix: make the table key depend on the filtered content, e.g. `key=f"projects_table_{hash(tuple(show.index))}"`, so the selection resets when rows change, and guard `sel = [i for i in sel if i < len(show)]`. For the cart, after Add/Remove also `st.session_state.pop("cart_table", None)` before `st.rerun()`.

### 2. HIGH. Grab & Go. MitoPilot map file is wrong for multi-project, nested, or lane-split carts
- Problem: the map lists bare basenames and the help says "give MitoPilot the copied data dir as its data_path", but the generated rclone job keeps the `<project>/<rel_path>` layout, so a cart spanning 2+ projects ends up in 2+ folders and no single data_path works. Lane-split samples (several R1 files) keep only the last R1/R2 seen; the others are dropped silently, even though the same page sums reads across lanes.
- Evidence: streamlit_app.py:1230-1237 (help text); mitopilot.py:30-33 (`by_sample[key][direction] = filename` overwrites); generated script from the demo cart lists `genohub-1001_Vietnam/...`, `genohub-2002_Hawaii/...`, `genohub-3003_Panama/...` under one DEST. load_samples r1_path/r2_path (streamlit_app.py:267-278) are scalar subqueries that also return one arbitrary lane file; in the 5-lane synthetic catalog, Samples "full R1/R2 paths" shows 1 of 5 files.
- Fix: write R1/R2 in the map as paths relative to DEST (`genohub-1001_Vietnam/x_R1.fastq.gz`) if MitoPilot accepts subpaths; otherwise add a "Flatten into one folder" option that generates per-file `rclone copyto` lines and blocks the map download when basenames collide. Warn when any sample has more than one R1 or R2 ("MitoPilot takes one file per read; 12 samples are lane-split, merge first").

### 3. HIGH. Grab & Go. Problem files flow silently into the cart, the map, and the copy job
- Problem: the cart table has no health column. Corrupt, empty, unchecked, and missing-from-disk files are copied with no warning. Blank read counts are not explained.
- Evidence: demo cart included genohub-2002_Hawaii 2002_S02 (R1 format_error, r1_reads shows `<NA>`) and genohub-3003_Panama 3003_S04 (project has no data folder). The copy section only says "2 file(s) have no recorded size (never stat'd on disk)" (streamlit_app.py:1261-1262), which is wrong for these files: they are not on disk at all. Table columns: streamlit_app.py:1176-1180.
- Fix: add a `files_ok` column (OK / 1 corrupt / missing on disk / unchecked) from files.integrity_status and data_check_issues, show a warning above "Copy sequence data" ("3 of 12 files are corrupt or missing; they will fail or copy bad data"), and add an "Exclude problem files" checkbox that is on by default.

### 4. HIGH. Global. Older catalog copies crash with a raw traceback on the default view
- Problem: the app tolerates missing columns in some loaders but not missing tables, and not the pre-integrity `files` columns. Projects is the default view, so the first thing a user with an older Scratch copy sees is a pandas DatabaseError.
- Evidence: AppTest old.db (dropped data_check_issues): Projects -> `no such table: data_check_issues` (load_data_issues, streamlit_app.py:353-363). old2.db (files without integrity_status/n_reads): Projects, Samples, Files, and Grab & Go all crash (`no such column: f.integrity_status`, streamlit_app.py:335-341, 456-459; `f.n_reads` at 472). Only Taxonomy survives.
- Fix: one startup check in main() comparing `PRAGMA table_info` with the expected tables/columns; on mismatch show `st.error("This catalog copy is older than this GUI (missing: data_check_issues). Ask your data manager to run 'seqledger init-db' on it, or re-sync the copy.")` and `st.stop()`. Wrap each view call in `except (sqlite3.Error, pd.errors.DatabaseError)` with the same friendly message.

### 5. HIGH. Grab & Go at scale. A large cart renders a multi-megabyte script on every rerun
- Problem: the whole job script, including every file path, is rendered with `st.code` on every interaction.
- Evidence: synthetic catalog, "Add all matches (5000)" then destination set: `st.code` payload is 2,546,491 characters (streamlit_app.py:1289). Server time stays low (0.43 s), but the browser has to syntax-highlight 2.5 MB on each rerun, including every later search keystroke.
- Fix: show the header plus the first 20 file lines in an expander ("Preview: 50,000 files total, full list in the download"), and keep the full text only in the download. Put the file list behind an `st.button("Generate script")` so it is not rebuilt on every rerun.

### 6. MEDIUM. Global. Locked catalog: about 63 s blank wait, then the wrong catalog name
- Problem: with the live master locked by a CLI write (rollback-journal mode, `pragma journal_mode` = delete), the page waits through `_config` (15 s busy_timeout, no retry) and then `_with_retry` (3 x 15 s plus backoff). The title falls back to the built-in default because `_config` swallows the error.
- Evidence: AppTest on lock.db held under `BEGIN EXCLUSIVE`: 63.4 s elapsed, title "Ocean DNA sequence data catalog" (not "Demo catalog"), then "Catalog is being updated - retry shortly." Code: streamlit_app.py:155, 162-164, 201-210, 1302. The only progress indicator is st.cache_data's default spinner, which shows the function name ("Running load_projects(...)").
- Fix: busy_timeout of about 2 s with 2 retries; `show_spinner="Loading projects..."` on each cache_data; cache the catalog name in session_state once it has been read; on lock, keep showing the last cached frames with an `st.warning("Catalog is being updated; showing data as of HH:MM")` instead of an empty page.

### 7. MEDIUM. Samples vs Grab & Go. Two search boxes with different meanings
- Problem: Samples sidebar search covers sample, taxon, UniqID, and WoRMS name, but not NCBI name or lineage. Grab & Go "Taxonomy" covers taxon, NCBI name, and lineage, but not WoRMS name. Regex exists only in Grab & Go. The Samples label "(sample, taxon, UniqID)" does not mention WoRMS.
- Evidence: streamlit_app.py:585-586 vs 1108-1110. AppTest: "Gadiformes" gives 0 of 42 in Samples and 6 matches in Grab & Go.
- Fix: one `_search_mask(df, q, regex)` used by both views over sample_id, uniq_id, taxon, tax_name, worms_name, lineage, and worms_lineage. Label it "Search sample, UniqID, or any taxon name/rank".

### 8. MEDIUM. Projects / Samples / Files. "Backup" means three different things
- Problem: Projects calls it "checksum" (per-file md5 rollup). Files "backup" is the per-file md5 match (Verified / Mismatch / Not compared). Samples "backup" is the project-level `backups.verified` flag, so one mismatched file marks every sample in the project "Not verified".
- Evidence: streamlit_app.py:260, 390-394, 456-457, 573. AppTest: all 15 genohub-1001_Vietnam samples show "Not verified", but only 1001_S09 R1 mismatched.
- Fix: derive the per-sample status from that sample's own files ("Backed up" / "Mismatch" / "Not compared") and use one column name everywhere ("Backup check"), with a column tooltip naming the backup location from config `backup_location`.

### 9. MEDIUM. Projects drill-down. Checksum mismatches are counted but never listed
- Problem: the Projects row says "1 mismatch", but the drill-down table only merges data_check_issues and integrity failures, so the user has to discover Files > Backup status = Mismatch on their own.
- Evidence: streamlit_app.py:696-699; AppTest: Vietnam drill-down lists 2 rows (gzip error, empty) and not the md5 mismatch.
- Fix: add `files WHERE md5_match = 0` rows to the merged table as kind "backup checksum mismatch", with a one-line explanation in `_ISSUE_DETAIL`.

### 10. MEDIUM. Status vocabulary drifts between views, the CLI, and the README
- Evidence:
  - data files: GUI "1 missing from disk, 1 missing from mapfile" (streamlit_app.py:405-407), CLI ingest output "data-files 1 missing, 1 orphan", README troubleshooting `data_files` = "N missing / M orphan" (README.md:447) and "N missing, M orphan" (README.md:376).
  - integrity: Projects says "corrupt" (437), the Files filter offers raw `gzip_error` / `format_error` (724), and the CLI prints "gzip-error".
  - empty 0-read files: "empty" in Projects (439) and the drill-down, but `integrity = ok` in Files, with no "empty" filter option (724).
  - match type: raw `fuzzy_species` in the Samples NCBI_match column, but "fuzzy (species)" in the Taxonomy chart (41-43).
- Fix: move the label maps into a shared module (for example seqledger/labels.py) used by CLI output, GUI cells, and the README table. Add "empty (0 reads)" and "missing on disk" to the Files integrity filter and show those words in the cell. Run tax_match through MATCH_LABELS in tables.

### 11. MEDIUM. Projects. Impossible states read as "not yet checked"
- Problem: a project with no data folder shows data_files "unchecked", integrity "unchecked", and checksum "incomplete (10 uncompared)". Its drill-down then says "No recorded data-files or integrity issues. Run validate ... and integrity --collect to refresh", right under a warning that no files are on disk. Rerunning those commands will never change anything.
- Evidence: AppTest, genohub-3003_Panama row and drill-down; streamlit_app.py:397-447, 713-714.
- Fix: when metadata_status is missing_seqdata, show "n/a (no data folder)" in all three columns and skip the "run validate" info.

### 12. MEDIUM. All views. Browse-only users are told to run data-manager commands
- Problem: the README's two-user model says GUI users only browse, but the GUI tells them to run `validate --seqdata-root`, `integrity --collect`, `taxonomy resolve --source worms`, and "Re-ingest (or re-sync the GUI copy)".
- Evidence: streamlit_app.py:122-124, 714, 851-852, 875-878.
- Fix: phrase these as "Ask your data manager to run `seqledger validate`", or put the command in a `help=` tooltip. On an empty catalog, the Taxonomy message should say "No samples in this catalog yet", not "Run taxonomy resolve" (AppTest empty.db).

### 13. MEDIUM. Tables. Column order, headers, units, and number types
- Evidence and problems:
  - Files puts `full_path` (about 150 characters on Hydra) before size, backup, and integrity (streamlit_app.py:739-741), so the status columns are off-screen on a laptop. The Samples files panel does the same (633-634).
  - `size` is a string "x.xx GB" (479-483, 738, 1163), so header sorting is lexicographic ("10.50 GB" sorts before "2.00 GB"), and small files show "0.00 GB".
  - `taxid` and `n_reads` load as float64 because of NULLs: AppTest shows taxid 6706.0 in the Files sample panel, n_reads 383.0 in the Files table, and the CSV exports write "383.0".
  - Headers mix snake_case and casing styles: n_samples, owner_name, data_files, date_ingested, uniq_id_link, NCBI_name, WoRMS_match, seq_data_size; plus "reads" and "checked" in Files.
  - UniqID is shown twice when it is a URL: the full URL text plus a separate "open" link column (591-593).
  - No column `help=` tooltips anywhere: grep finds no column_config help, NumberColumn, or ProgressColumn.
- Fix: keep size_bytes numeric and render with `st.column_config.NumberColumn("Size (GB)", format="%.2f")` on a GB float column; cast counts to Int64; use readable headers ("Samples", "Files", "Mapfile", "Data files", "Backup check", "Integrity", "Owner", "Ingested"); put status columns right after the ID columns and paths last; render UniqID as one LinkColumn when it is a URL; add `help=` to each status column with the one-line meaning (this doubles as the missing legend).

### 14. MEDIUM. Projects / Samples / Files. The per-column filter bar is hard to read and hides active filters
- Problem: up to 12 text boxes in one row with labels hidden. The column name is only a placeholder, so once you type you cannot tell which column a box filters. The expander starts collapsed, and its title does not change when filters are active, so a filtered table looks unfiltered apart from the "N of M" caption. There is no Clear button. Numeric columns get substring matching ("1" matches 1, 10, 12, 15). Sidebar search and column filters stack silently.
- Evidence: streamlit_app.py:556-565 (`label_visibility="collapsed"`, `expanded=False`).
- Fix: show small labels, title the expander "Filter columns (2 active)" and open it when any are active, add a "Clear all filters" button that resets the `colf_*` keys, and add a caption line "Filtered by: taxon contains 'gadus'; project = X".

### 15. MEDIUM. All views. Empty results and empty catalog show bare tables
- Evidence: AppTest search "zzz" in Projects shows "0 of 5 projects" and a 0-row table. empty.db: Projects, Samples, and Files show 0-row tables with no message.
- Fix: `if show.empty: st.info("No projects match. Clear filters?")` with a clear button. For an empty catalog: "This catalog has no projects yet. Your data manager needs to run `seqledger ingest`."

### 16. MEDIUM. Navigation. No deep links, and filters reset on every view switch
- Problem: navigation is an `st.sidebar.radio` with no query params. Sidebar filters and selections are dropped when you switch views; only the cart and the treemap root persist.
- Evidence: streamlit_app.py:1313-1314; grep finds no `query_params` or `st.navigation`. AppTest: Projects search "Viet" -> Samples -> back to Projects, and the search is "".
- Fix: st.navigation with st.Page per view, which gives each view its own URL, and mirror the key filters in `st.query_params` (`?project=...&sample=...`). Give sidebar widgets stable keys and copy their values into session_state (Streamlit drops state for widgets that are not rendered). Note: with `--qsub` the port changes every launch, so URLs are only stable within one session unless fix 22 is applied.

### 17. MEDIUM. Grab & Go. An invalid regex hides the cart and the copy section
- Problem: typing a half-finished pattern such as `Gad(` stops rendering the whole page below the error, including the cart, the exports, and the copy job.
- Evidence: streamlit_app.py:1100-1101 and 1115-1120 (`return`); AppTest: after `Gad(us` the only captions shown are the catalog line and the error.
- Fix: show the error, skip only that filter, and keep rendering the cart. Also, the 5,000-row regex cap (1102-1105) silently limits "Add all matches"; say "showing matches in first 5,000 of 5,312 samples".

### 18. MEDIUM. gui.py. A failed start still prints a tunnel to a dead server
- Problem: the qsub job writes READY after the 90 s wait loop whether or not Streamlit came up, so the launcher prints a tunnel to a server that is not running. That is the "connection refused" case in README troubleshooting.
- Evidence: gui.py:125-131 (the loop `break`s on success but falls through on timeout, then always echoes READY).
- Fix: `ok=0; for ...; do ... && { ok=1; break; }; done; [ $ok = 1 ] && echo READY ... || echo FAILED > ready`. The launcher should print the last 20 log lines on FAILED.

### 19. MEDIUM. gui.py. The GUI job's 2 GB virtual-memory cap is below the measured peak
- Problem: the GUI job defaults to `h_vmem=2G` (gui.py:103, mem_gb=2 at gui.py:137), but the app's virtual memory peaked at about 2.8 GB on the 50k-file catalog. Exceeding h_vmem kills the job or makes allocations fail, which shows up as a dead or broken GUI.
- Evidence: in-process AppTest walk of all views plus adding 5,000 samples to the cart: VmPeak 2,792,996 kB, VmHWM 348,036 kB. A real `streamlit run` server adds more threads, so its peak is likely higher. This was measured on this Linux workstation, not on Hydra.
- Fix: default `--mem 6`, or request mres/h_data without h_vmem if Hydra allows that. Mention the flag in the "GUI won't start" troubleshooting row.

### 20. MEDIUM. Taxonomy. Unreadable at thousands of taxa, and too many controls
- Evidence (synthetic 3,000-taxon catalog):
  - Composition rank = genus produced 1,779 legend entries on an 11-color `qualitative.Safe` palette that repeats (streamlit_app.py:937-938).
  - "Sample count by species" uses st.bar_chart, which sorts categories alphabetically and draws thousands of bars (909-911).
  - Sunburst at species depth with 8 levels: 1.25 MB figure spec, 5,341 nodes.
  - The "Levels shown" slider max is always len(rank_cols)=8, even when Deepest rank = order (861-862).
  - Seven sidebar controls for one page.
- Fix: bar = top 25 by count (horizontal plotly bar, sorted, plus an "other N" bar); composition = top 10 groups plus "other", and offer only domain to order; bind the slider max to depth; fold chart type, levels, and include-unknown into an "Chart options" expander. Add a "Show these samples" link from the selected clade to the sample search, prefilled.

### 21. LOW-MEDIUM. Grab & Go. Cart wording and feedback
- Problem: the same thing is called "custom table" (caption 1123, "click **Add to custom table**", but the button says "Add selected"), "Grab & Go table" (1124, 1149), and "cart" (code). The search results do not mark rows already in the cart. Adding gives no feedback except a counter. You cannot add from the Samples view, which has the richer filters. Remove only works by selecting rows and pressing the button.
- Fix: call it "Cart" everywhere, with a sidebar badge on every view ("Cart: 12 samples, 48.2 GB"); `st.toast("Added 6 samples (2 were already in the cart)")`; an "in cart" checkbox column in the results; and an "Add selected to cart" button under the Samples table.

### 22. LOW-MEDIUM. gui.py. Launch output and port handling
- Evidence:
  - local mode: if 8501 is busy, `_free_port` silently picks a random port (gui.py:38-55) with no message.
  - `--qsub` always uses a random remote port (gui.py:113), so the laptop URL changes on every launch.
  - Streamlit then prints its own "Collecting usage statistics", "Local URL", "Network URL", and "External URL" lines under the tunnel instructions (observed on this machine). In local mode they scroll the tunnel instructions away and suggest URLs that will not work from a laptop. Usage stats are not disabled, and the repo has no .streamlit/config.toml.
  - The "72h" note gives no stop time (gui.py:202-203).
  - Non-ASCII "middle dot" at gui.py:200.
- Fix:
  - Keep the laptop side fixed: print `ssh -N -L 8501:{node}:{remote_port} ...`, then "open http://localhost:8501". Add "if 8501 is busy on your laptop, change the first number".
  - Print "port 8501 busy on this node, using 41237" when it falls back.
  - Pass `--browser.gatherUsageStats false`.
  - Print "GUI stops automatically at 2026-10-18 16:07 (72 h)".

### 23. LOW-MEDIUM. gui.py plus Grab & Go. A forgotten GUI uses one of the two lTIO job slots for 72 h
- Problem: lTIO allows 2 concurrent jobs per user (gui.py:186, 202). A GUI left running holds one of them, which blocks the Grab & Go copy job and integrity batches that also need lTIO.
- Fix: add an idle watchdog in the job script, for example a background loop that exits when `ss -tn state established "( sport = :$PORT )"` shows no connections for 2 h. Also mention in the copy section: "this job needs a free lTIO slot; stop your GUI job (qdel N) if you have 2 running".

### 24. LOW-MEDIUM. Grab & Go. Copy-job ergonomics
- Problem:
  - "Download submission script" saves the .job file to the laptop, but it must run on Hydra (streamlit_app.py:1287-1291).
  - The file name `seqledger_rclone_copy.job` and the log `-o seqledger_rclone_copy.log` (rclone.py job header) are the same for every job, so two copy jobs in one directory write to one log.
  - `--progress` floods a batch log with carriage-return redraws.
  - The Transfers and Memory sliders are shown to every user.
- Fix:
  - The GUI already runs as the user on Hydra, so offer "Save to ~/seqledger_jobs/<slug>_copy_<YYYYmmdd-HHMM>.job" and print the exact `qsub` line. This writes no catalog data, so the GUI stays read-only.
  - Timestamp the job name and the log name.
  - Use `--stats 1m --stats-one-line -v` instead of `--progress`.
  - Move the sliders into an "Advanced" expander.

### 25. LOW. First impression and information architecture
- Problem: a new user sees the catalog name, an absolute path plus mtime caption, and a 12-column Projects health table with a dense paragraph caption (streamlit_app.py:674-677). There is no purpose line, no summary counts (projects / samples / files / TB / percent verified), and no pointer to the task most users came for (find samples, get data). That task is the last radio option, under the jargon name "Grab & Go".
- Fix: see (d). At minimum, add a one-line intro and an `st.metric` row (Projects, Samples, Files, Total size, Projects needing attention) above the radio views, and rename "Grab & Go" to "Get data (cart)".

### 26. LOW. Projects filters combine in a surprising way
- Problem: the "Only ... issues" checkboxes combine with AND (ticking two shows projects that have both). "Only checksum issues" counts never-compared files as issues (660), while "Only integrity issues" ignores unchecked files (663).
- Fix: one multiselect "Show projects with: mapfile problems / missing files / corrupt files / backup mismatch / not yet checked" combined with OR, with "not yet checked" kept as its own option.

### 27. LOW. Exports
- Problem: file names are constant (`{slug}_samples.csv`, `mitopilot_mapfile.csv` without a slug, `{sid}_files.csv`), so repeated downloads become "(1)", "(2)". Projects and Samples CSVs include helper columns (n_integrity_ok, backup_verified, uniq_id_url) and float-formatted counts.
- Fix: `{slug}_{view}_{YYYYmmdd}.csv`; export the displayed columns with display headers (Grab & Go already does this well with export_map, streamlit_app.py:1209-1221).

### 28. LOW. Detail panels
- Problem: the Files sample panel is a one-row, 14-column table (streamlit_app.py:764-776), which is hard to scan. The per-row owner privacy note repeats in two captions (677, 746).
- Fix: render the single record as a two-column key/value table or `st.container(border=True)` with labeled lines. Say the privacy note once in an About/Help expander.

### 29. LOW. Code hygiene against the repo's ASCII preference
- Problem: em dashes, a right guillemet, an ellipsis, and emoji in user strings, against the stated ASCII-only preference. Emoji are only decorative, since statuses are words, so accessibility is fine.
- Evidence: streamlit_app.py:236, 556, 904, 998, 1001, 1004, 1012, 1100, 1104, 1133, 1139, 1144, 1195, 1245, 1249, 1298, 1331; gui.py:200.
- Fix: swap them for ASCII ("-", ">", "...") and use Streamlit `icon=":material/add:"` for button icons.

## (c) Works well

- Read-only `mode=ro` connection that cannot auto-create a DB on a typo (streamlit_app.py:143-156). A missing DB gives a clear `st.error` (no hint about `--db` / SEQLEDGER_DB, though).
- Caching keyed on DB mtime: every view loads in 0.1 to 0.35 s at 5k samples / 50k files, and adding 5,000 samples to the cart took 0.5 s.
- Catalog identity and staleness caption (path plus last-updated time) on every view.
- Statuses are words, not color or emoji alone. The match-quality ramp is ordered and chosen for light and dark themes (streamlit_app.py:37-46).
- NCBI and WoRMS name link columns use the name as link text, and column filters match the displayed name, not the URL (streamlit_app.py:524-541).
- Plain-English mapfile explanations in the Projects drill-down. Integrity and disk issues are merged into one per-project table with a "detail" sentence for each row.
- CSV formula-injection guard on every export (streamlit_app.py:497-516).
- MitoPilot duplicate, empty-ID, and missing-read warnings with a "try a different ID column" hint (streamlit_app.py:1238-1250).
- The copy script is safe to run: DEST and SRC are shlex-quoted, it uses `rclone copy` (never sync), checks the destination before touching Store, can be resubmitted to resume (`--checksum`), and traps the wall-clock notify signal. An injection attempt in the destination field (`; rm -rf ~`) stayed a quoted string.
- Treemap progressive drill with Up/Top buttons, and the root is rechecked when filters change.
- The `--qsub` launcher prints a heartbeat with plain-English queue states and the `qdel` command.

## (d) Proposed information architecture

Use `st.navigation` with URL-addressable pages and a persistent header strip: catalog name, "42 samples / 96 files / 1.2 TB", "updated 2026-10-15 16:01", "3 projects need attention" (link), and a "Cart: N samples, X GB" badge.
1. Find samples (default page): one search box (sample, UniqID, any NCBI/WoRMS name or rank; regex under "Advanced"), facets (project, phylum/class, file health, backup check), and a results table with "Add to cart". Replaces Samples plus the Grab & Go search half.
2. Cart / Get data: cart table with a file-health column, then three clearly separated actions: Download table CSV, MitoPilot map (layout-aware), and Copy job (preview, save to ~/seqledger_jobs, qsub line).
3. Projects (health): status columns with header tooltips and a "What statuses mean" expander shared with the CLI. Clicking a project goes to `?project=X`, which shows tabs Samples | Files | Problems (mapfile, missing, corrupt/empty, checksum mismatch).
4. Taxonomy: fewer controls; a selected clade gives "Show these N samples", which opens Find samples prefilled.
5. All files (data-manager view, last): the current Files table, paths last.

## (e) Top 5 fixes

1. Make row selection safe: key the tables on the filtered rows and bounds-check `sel` (fixes the AppTest IndexError and the wrong-project panel; streamlit_app.py:625, 685, 756, 1135, 1197).
2. Add a schema-compatibility check at startup with a friendly "older catalog copy" message, and shorten the lock wait with a real "loading / being updated" message (findings 4, 6).
3. Fix Grab & Go correctness: layout-aware MitoPilot paths plus a lane-split warning, a file-health column with "exclude problem files", and a truncated script preview (findings 2, 3, 5).
4. Unify vocabulary and search: one label map shared with the CLI and README, one search function for Samples and the cart, one meaning of "backup", checksum mismatches listed in the drill-down, and "ask your data manager" instead of CLI commands (findings 7 to 12).
5. Table polish that biologists will notice: numeric Size (GB) and read columns, readable headers with `help=` tooltips, status columns before paths, and an active-filter indicator with "Clear filters" plus empty-result messages (findings 13 to 15).

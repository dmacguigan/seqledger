# seqledger correctness review (branch devel, cbd7759)

Repro scripts + throwaway DBs: /tmp/claude-1000/-home-dmacguig-Documents-GitHub-seqledger/3bcc3cd7-35f2-45f8-8e0b-6b4fa8b86b3c/scratchpad/correctness/ (r01..r14 *.py, run with plain `python rNN_*.py`).
Env: Python 3.12.10, pandas 3.0.3, streamlit 1.59.0. Suite: 183 pass, 1 fail (as expected).

## (a) Verdict

1. Core paths work on the happy path, but integrity can report a truncated file as OK after a routine re-ingest (critical, reproduced).
2. Re-ingest is not stable under real-world churn: a moved file forks a duplicate row, and a mapfile that is briefly missing or broken unlinks every file from its samples.
3. Batch integrity (`--batch` / `--collect`) re-uses stale results from earlier completed runs, so fixed or newly broken files are not re-read without `--force`.
4. The failing test is correct. The code is wrong: on pandas 3 the CSV formula sanitizer does nothing, including in production exports.
5. Tests cover single steps well but almost never cover "run, change the world, run again". Every high-severity bug below lives in that gap.

## (b) Findings by severity

### CRITICAL

**C1. Integrity reports a truncated file as OK after a re-ingest**
- Where: integrity.py:123-127 (same-size skip), ingest.py:315 (`size_bytes=COALESCE(excluded.size_bytes, ...)`)
- What goes wrong: the skip compares the size on disk with `files.size_bytes`. Ingest overwrites that value with the current size on every re-ingest. Integrity never records which size it actually checked. So after a file is truncated or overwritten, a routine re-ingest copies the new size into `size_bytes`, and the next integrity run sees "same size", keeps the old `gz_ok=1`, and skips the file. The project reports `pass`. Batch workers use the same cached state, so they miss it too.
- Repro (r13_skip_after_reingest.py): good file checks `ok` at size 122. Truncate it to 61 bytes; a direct check now says `gzip_error`. Run `ingest_tree`, then `check_catalog_integrity`: the row reads `('s1_R1.fastq.gz','ok',61)` and the project status is `pass`. CONFIRMED.
- Fix: have integrity store the size it verified (a new `integrity_size` column) and skip only when the disk size equals that. Or have `_upsert_file` clear `gz_ok`/`integrity_status`/`n_reads`/`md5_match` whenever the size changes. The second option also stops a stale `md5_match=1` from keeping a changed file's backup "verified".

### HIGH

**H1. Batch integrity re-uses finished results; `--collect` re-applies old JSON**
- Where: integrity.py:411-418 (`seed = _load_results_json(...)` is loaded even when a `.done` marker shows the prior run finished), integrity.py:450-486 (merges every `*.json` that has a `.done`, with no date check and no file_pk/project check), integrity.py:141-146 (`UPDATE ... WHERE file_pk=?` only).
- What goes wrong:
  - (a) Re-submitting `integrity --batch` without `--force` seeds every file from the last completed JSON, so nothing is re-read. A re-transferred good file stays `gzip_error`; a file that broke since the last run stays `ok`.
  - (b) `--collect` re-merges every old result file left in `results/`, stamping old statuses (with the old run_date) over newer local results.
  - (c) After a prune, SQLite can re-use a `file_pk` (INTEGER PRIMARY KEY, no AUTOINCREMENT). A stale JSON then writes one file's status onto a different file.
- Repro (r06_integrity.py): run1 gives R2 `gzip_error`. Fix R2; a direct check says `ok`. Run2 (default resume) still emits `gzip_error`. Separately, delete the max-pk row and insert a new file; it gets the same pk. Collecting an old JSON stamps `gzip_error, 2026-01-01` on the new, unrelated file. CONFIRMED.
- Fix: in `emit_project_json`, seed only when `.done` is absent (check it before removing it). In `collect_json`, add `AND project_id=?` to the UPDATE, and skip a result whose run_date is older than the row's `integrity_date`. Consider moving or archiving merged JSONs.
- Note: `test_emit_json_resume_skips_seeded_files` pins the seeding behaviour. It does not tell a partial checkpoint apart from a completed run.

**H2. A file that moves to another subdir becomes a duplicate row that `--prune` never removes**
- Where: ingest.py:290-304 (`disk_confirmed` moves a row only off the flat-guess path; a row at a different real path is treated as a separate file), ingest.py:474-485 (prune is basename-based).
- What goes wrong: when `run1/s1_R1.fastq.gz` moves to `run2/`, re-ingest inserts a new row. The old row keeps `sample_pk`, `direction=R1`, `md5_match=1`, and `integrity_status=ok`, so the sample now has two R1 rows:
  - the GUI `r1_path` subquery picks one arbitrarily;
  - Grab & Go and rclone try to copy a file that no longer exists;
  - `--prune` keeps the stale row because its basename is still in the mapfile.
- Repro (r05_ingest.py, A): after the move plus `ingest_tree(prune=True)` there are 4 rows. The 2 stale ones still say `md5_match=1, ok`. Validate flags them as missing, but nothing removes them. CONFIRMED.
- Fix: when a disk scan finds a basename at a new path and the existing row with that basename (same project, sample, and direction) no longer exists on disk, move that row (`UPDATE rel_path`) instead of inserting. Make prune delete by `rel_path`: drop rows whose `rel_path` was not produced by this run's disk/mapfile resolution.

**H3. A mapfile that is briefly missing, broken, or invalid unlinks every file from its samples**
- Where: ingest.py:579-609 sends these cases to `_ingest_diskonly`. That function (ingest.py:527-530) upserts every file with `sample_pk=None` and a guessed direction. ingest.py:313 then overwrites `sample_pk` and `direction` on conflict. ingest.py:517 sets `metadata_file` to NULL.
- What goes wrong: a mapfile renamed during an edit, saved with a bad header, or with all rows invalid, followed by one routine cron ingest, leaves every existing sample in place but with zero files. The GUI Samples view shows no files, and Grab & Go and MitoPilot exports come out empty. It only comes back when someone re-ingests a fixed mapfile.
- Repro (r05_ingest.py, B): before, `sample_pk=1` on both files. After renaming the mapfile and re-ingesting: `sample_pk=None` on both, the sample still exists, status is `missing_mapfile`. Same result with a broken header. CONFIRMED.
- Fix: in `_ingest_diskonly`, insert new files only. Leave `sample_pk`/`direction` alone on existing rows, for example `ON CONFLICT DO UPDATE SET size_bytes=..., owner_*=...` without touching `sample_pk`/`direction`. Keep the previous `metadata_file`.

**H4. The CSV formula sanitizer does nothing on pandas 3 (the failing test)**
- Where: streamlit_app.py:514 (`if out[c].dtype == object:`)
- What goes wrong: pandas 3 turns on `future.infer_string` by default, so text columns have the `str` dtype (StringDtype), not `object`. `pd.read_sql_query` also returns `str` dtype for TEXT columns. So every GUI CSV download (`_download`, plus Grab & Go at streamlit_app.py:1223) ships `=cmd()` cells unescaped. `requirements.txt` allows `pandas>=2.0`, so a fresh install gets pandas 3.
- Repro (r01_csv_safe.py): constructed column dtype is `str`. read_sql dtypes are `{'a': 'str', 'b': 'object'}`. The sanitized output still contains `'=HYPERLINK(1)'`. CONFIRMED.
- Fix: `if pd.api.types.is_string_dtype(out[c].dtype):`. This returns True for both `object` and `str`, and `_csv_safe_cell` already passes non-strings through. Verified: the patched version prefixes both cases and leaves int64 alone.

**H5. Ingest crashes with a traceback when the taxdump cannot be downloaded (offline node)**
- Where: taxonomy.py:103-107 wraps URLError/timeouts in `RuntimeError`. cli.py:273 catches only `(OSError, URLError, tarfile.TarError)`, and `main` (cli.py:834) catches only `(OSError, sqlite3.Error, ValueError)`.
- What goes wrong: the first ingest on a node without internet dies with a raw RuntimeError traceback after the metadata commit. The data-files check step never runs, so `data_check_status` stays NULL. The friendly "taxonomy skipped" message is unreachable.
- Repro (r09_ingest_tax_offline.py): with `urlopen` patched to raise URLError, the run raises `RuntimeError Failed to download NCBI taxdump...` and `data_check_status` is `None`. CONFIRMED.
- Fix: add `RuntimeError` to the except tuple at cli.py:273, or raise an OSError subclass from `_download`.

### MEDIUM

**M1. GUI crashes after a filter change while a row is selected**
- Where: streamlit_app.py:622-625 (Samples), 683-685 (Projects), 753-756 (Files), 1131-1137 and 1194-1198 (Grab & Go). All index the new filtered frame with old `selection.rows`.
- What goes wrong: a keyed `st.dataframe` keeps its identity from `key` + `selection_mode` only. Streamlit 1.59, elements/arrow.py:1044-1051, says outright that selections become orphaned when the data changes. Narrowing a search while a row is selected raises `IndexError: single positional indexer is out-of-bounds` and the whole view fails.
- Repro (r10_gui_selection.py, AppTest, with the frontend's persisted selection injected into widget state): select row 2, then search "Urophycis" (1 row left): exception. In Grab & Go, select row 0, then search to 0 matches: the same IndexError at line 1135. CONFIRMED in AppTest. The real-browser behaviour depends on the frontend keeping the selection, which Streamlit's own comment says it does.
- Fix: ignore out-of-range indices (`sel = [i for i in sel if i < len(show)]`). Better, make the key depend on the active filters so a new filter resets the selection.

**M2. `validate` never records "unchecked", so a deleted project folder keeps its old "OK"**
- Where: validate.py:286 (`if data["status"] != "unchecked": to_persist.append(...)`)
- Repro (r05_ingest.py, C): stored `ok`. `rm -rf` the project folder and validate again: it returns `unchecked`, but the stored value is still `ok`, so the GUI shows "OK". CONFIRMED.
- Fix: when the project's own data dir is missing (as opposed to the whole root being unmounted), persist `unchecked` (or a `missing_seqdata` style status) and clear `data_check_issues`. Keep skipping only when the root itself is absent.

**M3. The NCBI homonym guard is bypassed on the genus-anchored fuzzy path**
- Where: taxonomy.py:455 (`gtx = name_to_taxid(idx, genus)` returns the lowest taxid and has no homonym check). The exact path at 396-417 is guarded; this one is not.
- Repro (r08b_taxo_sorted.py, synthetic dump sorted by taxid like the real names.dmp): `"Morus basanus"` (a typo of the gannet) resolves to `fuzzy_genus taxid=3497 kingdom=Viridiplantae family=Moraceae`, which is the mulberry. CONFIRMED.
- Fix: use `scientific_name_taxids(idx, genus)` and, when there is more than one, try each genus's species list or return `ambiguous` through `disambiguate_homonym` with the caps as context.

**M4. The default review workflow marks every fuzzy guess as "confirmed"**
- Where: taxonomy.py:610-615 (`confirmed_taxid` pre-filled with `taxid` on every row), taxonomy.py:663. README line 309 tells users to "edit confirmed_taxid ... for any wrong fuzzy/unresolved rows" and then apply.
- What goes wrong: applying the CSV as documented promotes every untouched `fuzzy_species`/`fuzzy_genus`/`fuzzy_higher` row to `match_type='confirmed', confirmed=1`. The GUI shows it as the best quality, and `_upsert_taxon` (line 560) locks it against any later re-resolve.
- Repro (r08_taxo.py, second block): an untouched review of `Morus basanus` (fuzzy) gives `('Morus basanus','confirmed',1,...)`. CONFIRMED.
- Fix: pre-fill `confirmed_taxid` only for `exact`, or leave it blank and add an `accept` column. Either way, document that a filled cell means "I confirm".

**M5. Applying a review CSV saved by Excel does nothing, or crashes**
- Where: taxonomy.py:644 and worms.py:382 (`open(..., encoding="utf-8")`)
- What goes wrong: Excel's "CSV UTF-8" adds a BOM, so the first header becomes `\ufefftaxon`. `row.get("taxon")` is None and every row is skipped as "(blank taxon)". A cp1252 save with a non-ASCII author name raises UnicodeDecodeError, and nothing is applied. Ingest already handles both cases (`_read_text`, ingest.py:195-207).
- Repro (r14_review_bom.py): BOM file gives `(0, ["(blank taxon): row has confirmed_taxid '37699'"])`. The cp1252 file raises `UnicodeDecodeError`. CONFIRMED.
- Fix: open with `encoding="utf-8-sig"` and fall back to cp1252, reusing `ingest._read_text`.

**M6. Parked item 1: a taxon first resolved by WoRMS is never NCBI-resolved**
- Where: taxonomy.py:587-592, cli.py:123-126, query.py:24 (`match_type NOT IN (...)` drops NULLs), worms.py:300-304 (inserts a row with NCBI columns NULL).
- Repro (r02_parked.py): after `_upsert_worms`, the row reads `('Gadus morhua', None, None, 'unresolved')`. `_has_unresolved_taxa` returns False, the NCBI `scope="new"` list is `[]`, and `query taxa` returns `[]`. CONFIRMED.
- Fix: test `t.date_resolved IS NULL` instead of `t.taxon IS NULL` in all three places, and use `COALESCE(match_type,'unresolved') NOT IN (...)` in query.py.

**M7. Checksum matching breaks on uppercase hex, `./` prefixes, and Windows paths**
- Where: checksums.py:46-50 (no normalisation), 54-55 (`_project_of`), 114 (`s_md5 == p_md5` is case-sensitive).
- What goes wrong: the decoder explicitly supports PowerShell output (`Get-FileHash` prints uppercase hex), and every such file compares as a MISMATCH. A listing made with `md5sum ./...` or with backslash paths matches nothing. Every cataloged file is then treated as "absent" and its backup verification is revoked.
- Repro (r07_checksums.py): uppercase gives `md5_match=0, 'MISMATCH'`. A `./` prefix gives `md5_match=None` and `backups.verified=0`. Backslashes give an orphan warning. Spaces and the `*` marker parse correctly. CONFIRMED.
- Fix: lowercase the md5. Normalise the relpath with `relpath.replace("\\", "/")`, strip a leading `./`, and run `posixpath.normpath`.

**M8. The Grab & Go copy and the MitoPilot map do not work together; lane-split samples are silently cut**
- Where: rclone.py:93-103 (`--files-from` keeps the `<project>/<subdir>/` tree), streamlit_app.py:1236-1237 (help text says to use the copied dir as data_path), mitopilot.py:29-33 (one file per direction, the last one wins), mitopilot.py:43-59 (no warning). MitoPilot resolves reads as `file.path(data_path, files)` (MitoPilot/R/init_checks.R:350).
- What goes wrong: the copied files land at `DEST/<project>/<sub>/x.fastq.gz`. With `data_path=DEST` MitoPilot preflight reports "file(s) not found". A sample split across lanes (L001 + L002) gets only L002 in the map, with no warning.
- Repro (python snippet in scratchpad, same inputs as r10): the map row has only the `L002` files, `issues` are all zero, and the copy listing is `P/run1/...`, `P/run2/...`. CONFIRMED.
- Fix: copy flat (`rclone copy --files-from` into DEST, then flatten, or use per-file `copyto`), or tell the user the per-project data_path. Flag samples with more than one file per direction in `issues()`.

**M9. A permission error or I/O error is recorded as `gzip_error` (corrupt)**
- Where: integrity.py:36 (`_GZIP_ERRORS` includes `OSError`), 78-79.
- Repro (r06_integrity.py): a `chmod 000` file gives `{'status': 'gzip_error', 'detail': '[Errno 13] Permission denied...'}`. CONFIRMED. A transient NFS EIO during the read gets the same label (PLAUSIBLE).
- Fix: catch `PermissionError` and other non-gzip `OSError` during open/read separately and return `UNCHECKED` (or a new `read_error`), not `GZIP_ERROR`.

**M10. A mapfile row with an unquoted comma silently shifts columns**
- Where: ingest.py:217 (`{header[i]: raw[i] for i in range(min(len(header), len(raw)))}`)
- Repro (inline in scratchpad): the row `s1,R1,R2,Gadus morhua, juvenile,USNM 123` loads `uniq_id='juvenile'`. The real UniqID is dropped, status is `pass`/`ok`, no warning. CONFIRMED.
- Fix: when `len(raw) > len(header)`, emit a WARN and add a flag (for example `extra_cells`) to the sample.

**M11. Ctrl-C during a local `integrity` run waits for every queued file and loses recent results**
- Where: integrity.py:194 (`with ThreadPoolExecutor(...)`). On exit it calls `shutdown(wait=True)` without `cancel_futures`. Results after the interrupt are never persisted, and up to 199 uncommitted rows are rolled back.
- Repro (r12_ctrlc.py): 20 queued 0.5 s checks, SIGINT at 1 s. KeyboardInterrupt surfaces only at 5.0 s and all 20 files were read. CONFIRMED.
- Fix: wrap in try/except KeyboardInterrupt, call `ex.shutdown(cancel_futures=True)`, and `conn.commit()` before re-raising.

**M12. No non-zero exit codes for failures (cron cannot detect problems)**
- Where: cli.py:242 (ingest counts `n_fail` but returns 0), cli.py:556 (integrity with gzip errors returns 0), cmd_validate (issues still return 0), main cli.py:830-839.
- CONFIRMED by reading: no path returns non-zero apart from argparse errors, `sys.exit(msg)` guards, and caught exceptions.
- Fix: return 1 when any project FAILs (ingest) or any file is corrupt (integrity), and 2 for issues (validate), documented.

### LOW

**L1. Parked item 3: `--prune` counts names, not rows; same-basename stale rows survive.** ingest.py:477-485. Repro r03_prune.py: two `junk_R1` rows were deleted but `pruned_files=1`. The unlinked `L1/s1_*` rows survive because their basename is listed. CONFIRMED. The claim that prune deletes a still-present listed file is REFUTED: only basenames absent from the CSV are deleted. Fix: delete and count by `rel_path`/`file_pk`; use `cursor.rowcount` for the count.

**L2. Parked item 4: `--prune-projects` prints the wrong reason, and the cap cannot be overridden from the CLI.** cli.py:252-255 always prints "no projects discovered". Repro r04_cli.py: the real reason was "refusing to prune 2 of 3 projects ... (>50%); pass force=True". No CLI flag maps to `force`. CONFIRMED. Fix: `print(f"refusing to prune: {pr['reason']}")` and add `--force-prune-projects`.

**L3. Parked item 5: the batch summary always says `lTIO.sq`.** cli.py:501. Repro r04_cli.py: config `io_queue=myQ.q`; the script uses `#$ -q myQ.q` (correct) but the message says "submitted 3 job(s) to lTIO.sq". CONFIRMED. Fix: `print(f"submitted {len(job_ids)} job(s) to {io_queue}")`.

**L4. Parked item 2: `backup_location` is a dead setting.** It is written by init-db (cli.py:71) and never read. checksums.py:169, query.py:35, and streamlit_app.py:281 all hardcode `'pdrive'`. Repro r02_parked.py: config `nas` still writes `location='pdrive'`. Because all three places agree, no result is wrong; the knob simply has no effect. CONFIRMED. Fix: read `get_config(conn,'backup_location')` in all three, or remove the flag.

**L5. Glob metacharacters in the root or project name hide every file.** ingest.py:155, validate.py:197 (`glob.glob(os.path.join(data_dir, "**", pat))` without `glob.escape`). Repro r05_ingest.py, D: project `LAB-run[1]_X` catalogs flat-guess paths with no sizes, and validate reports both files "missing from disk" even though they exist. CONFIRMED. Fix: `glob.escape(data_dir)`.

**L6. A project with no mapfile shows data-files "OK".** validate.py:181-202 compares disk to the DB, not to the mapfile. Diskonly ingest has already put every disk file in the DB, so the label "missing from mapfile" can never fire for it. Repro r05_ingest.py, E: `ok` with metadata_status `missing_mapfile`. CONFIRMED. Fix: count rows with `sample_pk IS NULL` as orphans, or skip/flag the check for non-`ok` metadata_status.

**L7. A zero-length final read gives a false `format_error`.** integrity.py:83-84 (the "trailing blank line" allowance). Repro r06_integrity.py: `@r1\nACGT\n+\nIIII\n@r2\n\n+\n\n` gives `format_error, line count 7`. Trimmers can emit empty reads. CONFIRMED. Fix: allow the blank-line tolerance only when the raw count is not already a multiple of 4.

**L8. A legacy-catalog migration fails permanently on orphan FK rows.** db.py:208-209 (`INSERT INTO files_new ... SELECT` runs with `foreign_keys=ON`). Repro r11_migrate.py: a legacy files row with `sample_pk=999` (no such sample) makes `init_db` fail with `IntegrityError FOREIGN KEY constraint failed`. The table stays on the basename key, and every write command (each calls `init_db`) is blocked. Mechanism CONFIRMED; the trigger needs rows written while FKs were off (PLAUSIBLE). Fix: pre-check with `PRAGMA foreign_key_check(files)` and give a friendly message like the duplicate check, or null the orphan `sample_pk`s.

**L9. A NULL `metadata_detail` shows as "nan" in the Projects drill-down.** streamlit_app.py:689. `prow.get("metadata_detail")` is NaN under pandas 3 (r01: `nan truthy: True`), so the `_MAPFILE_DETAIL` fallback never runs. CONFIRMED (value level). Fix: `pd.notna(v) and v or _MAPFILE_DETAIL...`.

**L10. `taxonomy resolve --redo` does not do what its help and docstring say.** taxonomy.py:573 says "including confirmed ones", but `_upsert_taxon` (560) refuses to overwrite confirmed rows. The review CSV then lists machine results that were not written. CONFIRMED by reading. Fix: align the docs, or skip confirmed rows in the review output.

**L11. GUI match-type legends do not cover produced values.** worms.py:242 produces `review_phonetic`, `review_near_2`, and similar. The GUI order and colours (streamlit_app.py:57-66) list `phonetic`/`near_2`/`near_3`, which are never stored. NCBI `ambiguous` (taxonomy.py:413) is missing from MATCH_ORDER. The result is default colours and odd ordering. Cosmetic. CONFIRMED by reading.

**L12. `gui --qsub` readiness is not reliable.** gui.py:125-131 writes READY even when the 90 s wait loop timed out without a listener. gui.py:77-79 and 182: one failed `qstat` call is read as "job gone" and exits 1. PLAUSIBLE (reading only).

**L13. WoRMS cache key ignores `marine_only`.** worms.py:159/173. Repro r08_taxo.py: a `marine_only=True` miss is cached, and a later `marine_only=False` run never re-queries. CONFIRMED but latent: the CLI does not expose `marine_only`.

**L14. Lane-split samples show one arbitrary R1/R2 path in the GUI.** streamlit_app.py:267-278 (scalar subqueries over possibly more than one row). PLAUSIBLE (reading). Same root as M8.

**L15. Map-file mode ignores the map file's own dir when `metadata_root` is configured.** cli.py:175 resolves the config root before `ingest_map_file`, which overrides the documented default (ingest.py:670-671). CONFIRMED by reading; it only matters when a CSV is not found in the CWD.

## (c) Parked items and the failing test

| Item | Disposition |
|---|---|
| Failing test `test_csv_safe_df_only_touches_string_columns_and_does_not_mutate` | The TEST is right and the CODE is wrong. Root cause: pandas 3.0 defaults `future.infer_string=True`, so text columns are `str` dtype and `dtype == object` is False (streamlit_app.py:514). It also affects production, because `read_sql_query` returns `str` too. Fix: `pd.api.types.is_string_dtype(out[c].dtype)` (True for both object and str). See H4. |
| (1) WoRMS-first row blocks NCBI | CONFIRMED (M6). Also hides the taxon from `query taxa` (query.py:24). |
| (2) backup_location ignored | CONFIRMED as a dead setting (L4). No wrong result today, because all three sites hardcode the same `'pdrive'`. |
| (3) prune by basename | PARTLY CONFIRMED (L1): `pruned_files` undercounts, and stale same-basename rows are never pruned. REFUTED: it does not delete a still-present file that the CSV lists. The bigger related bug is H2 (a moved file forks a row that prune keeps). |
| (4) prune-projects message | CONFIRMED (L2), plus the cap has no CLI override. |
| (5) lTIO.sq in message | CONFIRMED (L3). Cosmetic only; the generated script uses the configured queue. |

## (d) Test gaps

Missing behaviour tests (each one would have caught a finding above):
1. Integrity after "check ok, then file changes size, then re-ingest, then integrity" (C1).
2. Batch: emit, mark done, fix a file, emit again without --force; and collect with an old JSON present or a re-used file_pk (H1). The existing resume test only covers the no-.done seed.
3. Re-ingest after a file moves to another subdir, with and without --prune (H2).
4. Re-ingest after the mapfile is removed, gets a bad header, or has all rows invalid, asserting `sample_pk` survives (H3).
5. A `_csv_safe_df` test on a frame from `pd.read_sql_query` (the real input), not only a constructed frame (H4).
6. CLI ingest with the taxdump download failing (H5). The existing download tests stop at `_download`.
7. Any Streamlit AppTest coverage at all. No test runs the app; selection/filter interaction (M1) and the `nan` detail (L9) are untested.
8. Validate after the project folder is deleted (M2).
9. Homonym genus on the fuzzy-species path (M3); applying an untouched review CSV (M4); a review CSV with a BOM or cp1252 (M5).
10. A WoRMS-then-NCBI ordering test for `scope="new"`, `_has_unresolved_taxa`, and `query taxa` (M6).
11. md5 listings with uppercase hex, `./` prefix, backslashes, spaces in names, and the `*` marker (M7). Spaces and `*` work but are untested.
12. Lane-split samples in `build_map_rows`/`issues`, and copy-layout vs MitoPilot data_path (M8).
13. Unreadable file (permission) in integrity (M9); zero-length final read (L7).
14. Mapfile rows that are longer or shorter than the header, swapped R1/R2 columns, single-end (empty R2), and paths with spaces in the two-column map file. `read_map_file` uses `split()`, so a path with a space breaks silently.
15. CLI output and exit codes: the prune-projects refusal reason, the batch queue message, and non-zero exits (L2, L3, M12).
16. Glob metacharacters in project names (L5); a diskonly project's data-files status (L6).
17. Migration with orphan FK rows (L8); `init-db` running while a read-only connection holds a read transaction.

Tests that assert implementation rather than behaviour:
- `test_emit_json_resume_skips_seeded_files` pins "trust any seed", which is the H1 bug's behaviour.
- `test_prune_deletes_stale_file_rows` asserts `pruned_files == 1` and its comment relies on "filename is UNIQUE per project", which is no longer true.
- `test_csv_safe_df_...` names the intent ("string columns") but was written against the `object` dtype detail.

Flaky or slow patterns:
- `test_free_port_*` (tests/test_gui_cli.py:72-99) binds real ports and asserts a just-released port can be bound again. That races on a busy CI host.
- `test_with_retry_*` sleep for real (`_LOCK_BACKOFF` is not monkeypatched; about 3 s in the persistent-lock case). Slow, not flaky.

## (e) Top 5 fixes

1. **C1**: have integrity record the size it verified, and skip only when the disk size equals that recorded size. Also reset integrity and md5 state in `_upsert_file` when `size_bytes` changes.
2. **H1**: seed `emit_project_json` only when no `.done` exists. Make `collect_json` update `WHERE file_pk=? AND project_id=?` and skip results older than the row's `integrity_date`.
3. **H3 + H2**: stop `_ingest_diskonly` from overwriting `sample_pk`/`direction` on existing rows. Move a row whose old path vanished instead of inserting a new one, and prune by `rel_path`.
4. **H4**: one-line dtype fix in `_csv_safe_df` (`pd.api.types.is_string_dtype`). This also turns the suite green.
5. **H5 + M12**: catch `RuntimeError` around ingest's taxonomy step, and return non-zero exit codes for FAIL, corruption, and validate issues so cron can tell when something broke.

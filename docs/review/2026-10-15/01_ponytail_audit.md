# Ponytail audit: seqledger (devel)

Core is mostly honest stdlib code. The fat is in four places: legacy-schema
compatibility (DB migrations plus GUI shims for old catalog copies), NCBI/WoRMS
copy-paste, a Streamlit app that grew a second copy of every feature, and hardcoded
defaults repeated in 7 places. Ranked biggest cut first. Deltas are source lines
unless marked.

## Ranked findings

1. yagni: one-shot legacy migrations (`_MIGRATIONS` 37 cols already in schema.sql, role/read -> direction rename, basename -> rel_path files rebuild with `_FILES_REBUILD_DDL` + `_files_unique_keys`). Run `init-db` once on the master (and re-sync replicas), then delete; move `idx_files_mismatch` into schema.sql. [seqledger/db.py:102-252] (-135, plus ~-80 in tests/test_db.py)
2. yagni: progressive Treemap drill-down (session-state root, Up/Top buttons, replay-dedup token) next to a Sunburst that already drills client-side. Keep Sunburst, drop the chart-type radio and `_treemap_drilldown`. [seqledger/app/streamlit_app.py:857,891-901,976-1029] (-70)
3. yagni: `query.py` is a layer with one caller: six one-statement functions plus a 6-branch if/elif in `cmd_query`. Replace both with one dict `{what: (sql, cols)}` in cli.py and a 3-line dispatch. [seqledger/query.py:1-59, seqledger/cli.py:559-581] (-45)
4. native: app-level lock retry (`_with_retry`, `_CatalogLocked`, `_is_locked_error`, 3 constants, try/except in main) on top of a 15 s `busy_timeout` that already waits out locks; and `_ro_connect` duplicates `odb.connect_ro`. Use `odb.connect_ro(path)` with `sqlite3.connect(..., timeout=15)`, which is SQLite's busy handler. [seqledger/app/streamlit_app.py:143-198,1315-1332, seqledger/db.py:87] (-45)
5. yagni: legacy two-column map-file ingest (`read_map_file`, `ingest_map_file`, positional `map_file`, metadata_root resolution, CLI branch) now that auto-discovery `ingest_tree` covers real layouts. Delete if no live layout needs it; tests must move to `ingest_tree` (49 call sites). [seqledger/ingest.py:176-192,662-678, seqledger/cli.py:195-199,249-250,706-708] (-45)
6. shrink: NCBI and WoRMS are the same pipeline copy-pasted: `_dedup` x2, `_blank` x2, scope SQL triplet x2, `write_review_csv` x2, `apply_review` row loop x2, upsert/apply-confirmed x2. One `_dedup = list(dict.fromkeys(filter(None, xs)))`, one `_taxa_in_scope(conn, scope, confirmed_col, date_col)`, one review writer `DictWriter(..., extrasaction="ignore")` over `{k: "" if v is None else v}`, one apply loop taking a lookup callback. [seqledger/taxonomy.py:362-368,512-518,564-669, seqledger/worms.py:127-133,197-204,287-409] (-50)
7. yagni: Grab & Go regex toggle plus its ReDoS scaffolding (`_validate_regex`, `_MAX_REGEX_LEN`, `_MAX_REGEX_ROWS`, row cap, two except arms). Plain substring search is what biologists type. [seqledger/app/streamlit_app.py:1032-1061,1081-1120] (-40)
8. delete: GUI shims for unmigrated read-only copies: `_table_columns` guards in `load_samples`/`load_taxonomy`/`load_projects`, legacy 'missing'/'orphan' issue kinds, 'unknown' mapfile status, `_MAPFILE_DETAIL` (duplicates ingest's detail strings already stored in `metadata_detail`), try/except in `get_config`/`resolve_config`. Same precondition as #1. [seqledger/app/streamlit_app.py:89-140,241-256,288-293,303-323, seqledger/db.py:45-48,65-70] (-40)
9. shrink: three views repeat one scaffold: dataframe with on_select, `_download(... catalog_slug ...)`, `event.selection.rows` guard, and the NCBI/WoRMS LinkColumn config block pasted 3x. One `_table(df, cols, key, csv_name, config)` helper returning the selected row, plus one `_TAXA_LINK_CONFIG` dict. [seqledger/app/streamlit_app.py:607-622,678-683,747-753,610-619,770-776,1185-1193] (-30)
10. shrink: init-db has 10 named flags plus `_INIT_CONFIG_FLAGS` mapping that only re-spell `--set KEY=VALUE`. Keep `--set` (maybe `--seqdata-root`/`--metadata-root`), drop the rest. [seqledger/cli.py:64-72,81-84,681-696] (-28)
11. shrink: two search UIs per view (sidebar "Search" box plus per-column filter boxes) and three substring matchers (`_contains`, the body of `_filter_by_columns`, `_match`) with the same fillna/astype/str.contains. Keep the column filters and one matcher. [seqledger/app/streamlit_app.py:486-492,531-541,574-587,645-654,725-736,1032-1038] (-25)
12. delete: `validation_log` table is write-only in production (only tests read it), `report_relpath` is never written, `projects.notes`/`backups.notes` are never read or written. Drop the table, `_log_run`, and the `log=` param. [seqledger/schema.sql:38,141,155-162, seqledger/integrity.py:149-152,261-305] (-18)
13. shrink: two tolerant text decoders: `_read_text` (utf-8-sig, cp1252) and `_decode_md5_file` (5-entry BOM table incl. UTF-32). One `read_text(path)` in db.py: `raw.decode("utf-16")` when it starts with `\xff\xfe`/`\xfe\xff` (the codec strips the BOM), else `utf-8-sig`, falling back to `cp1252`/`latin-1`. [seqledger/ingest.py:195-207, seqledger/checksums.py:6-28] (-18)
14. shrink: three SGE qsub headers (`_batch_script`, `build_copy_script`, `_gui_job_script`) repeat the same `#$` block and the bashrc/conda lines. One `qsub_header(name, log, queue, mem, slots=None)`. [seqledger/cli.py:401-433, seqledger/rclone.py:107-123, seqledger/gui.py:96-110] (-15)
15. shrink: hardcoded defaults repeated in 7 places (`rclone.RCLONE_MODULE`, `gui.LOGIN_HOST`, "lTIO.sq" x5, "seqledger" x4, `_DEFAULT_FASTQ_GLOBS`, the inline glob fallback in validate). Read `CONFIG_DEFAULTS`; make `fastq_globs` fall back itself. [seqledger/rclone.py:17,72, seqledger/gui.py:20,136-137, seqledger/cli.py:402,436,501,657-659, seqledger/ingest.py:38,171-173, seqledger/validate.py:193] (-12)
16. shrink: `load_files` and `load_file_paths` are the same query minus three columns. Use one superset query. [seqledger/app/streamlit_app.py:450-476] (-12)
17. shrink: status labels exist three times: `_data_label`/`_checksum_label` (CLI), `data_files_label`/`checksum_label` (GUI), and the `check_checksums` status ladder. One pair in validate.py, used by both. [seqledger/cli.py:339-359, seqledger/app/streamlit_app.py:397-418, seqledger/validate.py:205-228] (-12)
18. stdlib: hand-rolled uid->name cache plus a `pwd` ImportError guard on a Linux-HPC-only tool. Use `@functools.lru_cache` on `lambda uid: pwd.getpwuid(uid).pw_name` and drop the `uid_cache` threading through 3 functions. [seqledger/ingest.py:29-32,103-123,361,507] (-10)
19. shrink: `cmd_ingest` taxonomy step re-implements `_resolve_ncbi` (resolve, write review CSV, print counts, print next step). Call one helper. [seqledger/cli.py:262-285,588-600] (-10)
20. shrink: NaN/None helpers twice (`rclone._missing`, `mitopilot._s`), `human_size` twice (rclone and app), `MITOPILOT_COLUMNS` equals `db.REQUIRED_COLUMNS`, `estimate_size` `hasattr(r, "get")` branch when rows are always dicts. [seqledger/rclone.py:20-29,59, seqledger/mitopilot.py:10,13-17, seqledger/app/streamlit_app.py:479-483] (-10)
21. shrink: `check_checksums` and `checksums._update_backup` run the same COUNT/SUM aggregate. One `_md5_counts(conn, pid)`. [seqledger/validate.py:211-219, seqledger/checksums.py:156-165] (-8)
22. shrink: rank/URL constants re-typed in the app: `RANK_COLS`, `RANK_LABELS`, `WORMS_RANK_COLS`, `WORMS_RANK_LABELS`, `WORMS_TAX_URL` (`worms.WORMS_TAXON_URL` is defined and unused). Import `taxonomy.RANK_COLUMNS/RANKS` and `worms.WORMS_RANK_COLUMNS/WORMS_RANKS/WORMS_TAXON_URL`. [seqledger/app/streamlit_app.py:32-35,51-55, seqledger/worms.py:29] (-8)
23. shrink: cart is a dict of `"pid\tsid" -> {project_id, sample_id}` plus `_cart_key`, rebuilt into a set of tuples anyway. Store a set of `(pid, sid)` tuples. [seqledger/app/streamlit_app.py:1064-1065,1077,1133-1146,1154,1195-1198] (-8)
24. delete: gui.py removes a `ready` file whose name has a fresh pid+timestamp (it never exists), and the 6-line DEFERRED bind comment is pasted twice. [seqledger/gui.py:58-65,115-120,150-151] (-8)
25. delete: `isal` optional fast path, which is not a declared dependency; the module docstring itself says the network read dominates. Plain `gzip`. [seqledger/integrity.py:31-41] (-7)
26. shrink: `check_data_files` has a `seqdata_root is None` DB lookup that no caller hits (validate_catalog always passes a root) and returns the same "unchecked" dict twice. [seqledger/validate.py:175-189] (-7)
27. stdlib: `Finding` class with hand-written `__init__`/`__repr__`. `Finding = namedtuple("Finding", "level message")`. [seqledger/validate.py:18-24] (-6)
28. shrink: `is_high_confidence` has a 6-line docstring for a one-liner; `_cache_path` has one caller. Inline both. [seqledger/worms.py:47-54,93-94] (-6)
29. shrink: `load_samples` uses 4 correlated subqueries (r1_path, r1_owner, r2_path, r2_owner). Use two `LEFT JOIN files r1/r2 ON ... direction='R1'/'R2'`. [seqledger/app/streamlit_app.py:267-278] (-6)
30. delete: `worms._fetch_json` has an unreachable `if last: raise last; return None` tail, and an `HTTPError` 204 branch that urllib never raises (2xx passes through). [seqledger/worms.py:74-76,88-90] (-5)
31. delete: `_DELETE_CHUNK` chunking around `executemany`, which binds 2 params per row and can never hit SQLITE_MAX_VARIABLE_NUMBER. One `executemany`. [seqledger/ingest.py:39-40,480-483] (-4)
32. shrink: the epithet extraction in `_resolve_one` step 2 re-implements `bare_epithet`. Call it. [seqledger/taxonomy.py:451-453] (-4)
33. delete: `cli.py` shebang plus `if __name__ == "__main__"` duplicates `__main__.py` and the console script; the `icons` dict is defined twice. [seqledger/cli.py:1,169,386,842-843] (-4)
34. shrink: `_config(DB_PATH, key)` opens a fresh connection on each of 7 calls. One cached `odb.resolve_config` dict per run. [seqledger/app/streamlit_app.py:201-210] (-4)
35. delete: `resolve_catalog(redo=)` is a "legacy alias" that no caller passes. [seqledger/taxonomy.py:564,574-577] (-3)
36. delete: `checksums --source ingest|backfill`: ingest never hashes, so 'ingest' is a dead choice. Hardcode 'backfill' or drop `md5_source`. [seqledger/cli.py:733, seqledger/checksums.py:58,122] (-3)
37. delete: `backup_location` config key and `--backup-location` flag: nothing reads it ('pdrive' is hardcoded everywhere). [seqledger/db.py:34, seqledger/cli.py:71,693-694] (-3)
38. shrink: `parse_md5sum` calls `rstrip("\n")` after `splitlines()` (a no-op), and its blank-line check is covered by `len(parts) != 2`. [seqledger/checksums.py:39-45] (-3)
39. shrink: `_cached_state` copies 4 fields out of each Row into a dict. `{r["file_pk"]: dict(r) for r in rows}`. [seqledger/integrity.py:169-173] (-3)
40. shrink: `_broken_mapfile_detail` uses a `.format` template constant plus a wrapper. One f-string. [seqledger/ingest.py:52-61] (-3)
41. delete: a duplicate `import re` inside `custom_table_view`, and a `sys.path.insert` hack in an installed package. [seqledger/app/streamlit_app.py:26,1075] (-3)
42. delete: `prune_missing_projects(force=)` has no caller, and its refusal text tells the user to "pass force=True", which the CLI cannot do. [seqledger/ingest.py:620,653-656] (-2)
43. shrink: `jobs = min(8, os.cpu_count() or 1)` is written twice. Use a module constant or argparse default. [seqledger/integrity.py:333-334,406-407] (-2)
44. stdlib: `socket.timeout` is an alias of `TimeoutError` on 3.10+, so drop it and the `socket` import. [seqledger/taxonomy.py:13,103] (-1)

## Tests
45. native: `_fresh_db`/`_fresh` is pasted in 4 files. Make it one `conftest.py` fixture. [tests/test_db.py:13, tests/test_ingest.py:9, tests/test_ingest_tree.py:11, tests/test_config.py:9] (-12)
46. shrink: identical `_setup` in two files. Move it to helpers.py. [tests/test_integrity.py:12, tests/test_validate_catalog.py:10] (-9)
47. delete: a fake-streamlit stand-in, though streamlit is in the `test` extra. [tests/test_gui_app.py:27-45] (-15)
48. delete: `_load_cli()` returns an already-imported module; `_gz` duplicates the gzip writer in `make_project`. [tests/test_ingest_pipeline.py:9-11, tests/test_ingest_tree.py:24-27] (-7)
49. native: the conftest `sys.path.insert`. Use `[tool.pytest.ini_options] pythonpath = ["."]` (2 lines) or the editable install. [tests/conftest.py:1-5] (-3)

## Packaging
50. delete: requirements.txt repeats `pyproject` `[gui]`/`[test]` extras word for word. Use `pip install -e .[test]`. Optionally point environment.yml at the same via `pip: ["-e .[test]"]`. [requirements.txt] (-7)
51. delete: the `"seqledger.app" = ["streamlit_app.py"]` package-data entry; setuptools already ships `.py` files of listed packages. [pyproject.toml:37] (-1)
52. native: plotly is only needed for the sunburst/treemap and one colored hbar. Pandas comes with streamlit anyway. If #2 lands and the sunburst is swapped for `st.bar_chart` drill-down by rank, plotly goes. Otherwise keep it. [pyproject.toml:23, seqledger/app/streamlit_app.py:844] (-1 dep, conditional)

net: -825 lines, -1 deps possible.

(#1, #5, and #8 are the big ones and all depend on one fact: every live catalog has
been through `init-db` on current code, and nobody uses the two-column map file. Confirm
that, then cut. Of the rest, #3, #4, #6, #13-#15, and #31 are safe and mechanical.
#2 and #7 are product calls.)

## Parked for the correctness round
- NCBI `scope="new"` and the CLI's `_has_unresolved_taxa` both test `t.taxon IS NULL`, so a taxon whose `taxa` row was first created by a WoRMS resolve (NCBI columns NULL) is never NCBI-resolved. [seqledger/taxonomy.py:587-592, seqledger/cli.py:123-126, seqledger/worms.py:300-304]
- The `backup_location` config is advertised but ignored: 'pdrive' is hardcoded in `_update_backup`, `unbacked_projects`, and the GUI `load_samples` join. [seqledger/checksums.py:169, seqledger/query.py:35, seqledger/app/streamlit_app.py:281]
- `--prune` deletes stale files by basename (`filename`), which is no longer unique. It can remove a still-listed same-basename file in another subdir, and `pruned_files` counts names, not rows; the "filename is UNIQUE" comment is stale. [seqledger/ingest.py:477-485]
- `--prune-projects` prints "no projects discovered" for every refusal, including the >50% blast-radius cap and missing-root cases; `pr["reason"]` is never shown. [seqledger/cli.py:253-255]
- `_submit_batch` prints "submitted N job(s) to lTIO.sq" regardless of the configured `io_queue`. [seqledger/cli.py:501]
- Non-ASCII in code against house rules: em dashes, arrows, and emoji in streamlit_app.py strings (e.g. lines 556, 904, 998-1012, 1100, 1104, 1133-1195, 1245-1249, 1298, 1331), and a middle dot in gui.py:200.
- pyproject `authors` lists "Claude (Anthropic)", against the no-attribution rule. [pyproject.toml:14]

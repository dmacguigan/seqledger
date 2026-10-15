# seqledger: operational review (data manager / HPC sysadmin view)

Repo: seqledger @ devel (cbd7759). All claims below were read in code; items marked [verified] were reproduced in a scratch catalog
(/tmp/claude-1000/-home-dmacguig-Documents-GitHub-seqledger/3bcc3cd7-35f2-45f8-8e0b-6b4fa8b86b3c/scratchpad/datamgr/).
Test suite here: 183 passed, 1 failed (CSV-injection sanitizer under pandas 3.0.3, see #14).

## (a) Verdict

1. Day-to-day happy path is good: ingest is idempotent, re-runs are cheap, batch workers never write the DB, first-run errors are clear.
2. Two silent-wrong-answer bugs hit the core promise: a per-project md5 load wipes every other project's backup verification, and a replaced-then-reingested file is reported integrity OK without being read.
3. Batch integrity resume/collect has no notion of staleness: old JSON results are reused forever and re-collect overwrites newer results.
4. Database management is close to absent: no backup/snapshot command, no schema version, migrations run implicitly from every write command and are not atomic, no writer lock.
5. Not cron-safe yet: exit code is 0 on integrity FAIL, md5 mismatch, and refused prune; fix exit codes, checksums scoping, and integrity staleness before trusting it as the source of truth.

## (b) Findings, ranked by operational risk

1. **critical** - `checksums` resets every cataloged file that is absent from the md5 listing, catalog-wide, unless `--project` is passed.
   Scenario: you follow README "run it per project in the background" (README.md:336-337), produce `p3.md5` for one project, and run `checksums --store p3.md5 --pdrive p3.md5` without `--project`; 31 of 37 files in 6 other projects drop to md5_match NULL, all `backups.verified` go to 0, and a recorded mismatch is erased (n_mismatch 1 -> 0). The "truncated file?" warning fires but the run still commits and exits 0. Same wipe happens for a listing made from inside the project dir (wrong root: paths lack the project prefix). [verified]
   Where: seqledger/checksums.py:83-92 (warn only), 129-144 (reset loop), 149 (commit); seqledger/cli.py:327-336 (exit 0).
   Fix: only reset files under projects that actually appear in the listing (or require `--full` for catalog-wide), and abort without committing when the truncation heuristic fires unless `--force`.

2. **critical** - Integrity skips a re-ingested file that changed on disk and keeps reporting it OK.
   Scenario: a file passes integrity, is later replaced by a truncated copy (failed re-transfer), nightly `ingest` refreshes `size_bytes` to the new size, then `integrity` sees gz_ok=1 and size == size_bytes and skips it: truncated file reported `ok`. [verified with Proj6 S1_R1]
   Where: seqledger/ingest.py:307-319 (size updated, integrity columns untouched); seqledger/integrity.py:123-127 (same-size skip trusts files.size_bytes).
   Fix: record the size (and mtime) observed at check time in its own columns (e.g. `integrity_size`, `integrity_mtime`) and compare against those, or have ingest reset `gz_ok/integrity_status` when size or mtime changes.

3. **high** - Batch resume JSON never expires, and `--collect` re-applies old JSON over newer results.
   Scenario A: months later you `integrity --batch` again; each job loads its old `results/<proj>.json` as a seed and skips those file_pks without even a stat, so a corrupted/replaced file stays `ok`, and old `gzip_error`/`unchecked` results are never retried. Only `--force` escapes. [verified]
   Scenario B: a local run finds Proj1 S2_R1 corrupt (FAIL); a later `--collect` over the same results dir re-merges the old JSON and flips it back to `ok`. [verified]
   Also: collect writes by bare file_pk with no check that the pk still belongs to that project (pk reuse after prune) and no size check.
   Where: seqledger/integrity.py:411 (seed loaded even after a completed run), 416-418, 458-487 (merge everything, no date/project/size check, JSON never archived).
   Fix: seed only when no `.done` exists (true resume), store size per result and re-stat seeded files, move JSON+`.done` to `results/merged/` after collect, and `UPDATE ... WHERE file_pk=? AND project_id=? AND (integrity_date IS NULL OR integrity_date <= ?)`.

4. **high** - Any OSError while opening/reading a file is recorded as `gzip_error` (corrupt).
   Scenario: data manager runs integrity as their own user over a colleague's mode-600 file, or NFS throws ESTALE/EIO, or a file is moved between stat and open: the file is marked corrupt (gz_ok=0) and the project FAILs. [verified with chmod 000]
   Where: seqledger/integrity.py:36 (OSError in _GZIP_ERRORS), 78-79.
   Fix: catch OSError from open/read separately and record a distinct `unreadable` (or keep prior result for transient errnos), reserving `gzip_error` for zlib/BadGzipFile/EOFError.

5. **high** - Exit codes do not reflect outcome, so nothing is cron/alert-able.
   Scenario: `integrity` with a FAIL project exits 0 [verified]; `--prune-projects` refusal exits 0 [verified]; `checksums` with mismatches or the truncation warning exits 0; `validate` with issues exits 0; `integrity --batch` with no qsub on PATH exits 0 [verified]; per-project `qsub failed` exits 0.
   Where: seqledger/cli.py:552-556, 362-379, 327-336, 247-260, 483-492.
   Fix: return distinct nonzero codes (e.g. 1 error, 2 data problems found, 3 refused) and print a final one-line summary with a timestamp.

6. **high** - Schema migrations run implicitly from every write command, are not atomic, are unversioned, and can leave the catalog half-migrated.
   Scenario: after `git pull`, the nightly cron `ingest` silently migrates the production catalog (README only warns about `init-db`, README.md:97-98). On a legacy catalog with one row whose direction is not R1/R2, or a file row whose project was deleted, the rebuild dies with `CHECK constraint failed` / `FOREIGN KEY constraint failed`; the ADD COLUMNs and role->direction rename are already committed, `files_new` is left behind, and every subsequent write command fails the same way until someone fixes it by hand in SQL. [verified both] `init-db --show` (documented as "print and exit") also migrates and applies flags.
   Where: seqledger/db.py:218-252 (each ALTER autocommits), 183-215 (pre-check covers duplicates only), 198 (RuntimeError not caught by cli.py:834 so it is a traceback); seqledger/cli.py:77, 171, 329, 525, 623 (init_db on every write path), 95-99 and 699 (`--show`); no PRAGMA user_version anywhere (reads 0).
   Fix: track `PRAGMA user_version`, make write commands refuse (not auto-migrate) when the version is old, and make an explicit `migrate` that first writes a backup, pre-checks CHECK/FK violations, and runs inside one BEGIN IMMEDIATE ... COMMIT.

7. **high** - No backup, snapshot, integrity_check, or writer-lock story for a SQLite file on NFS.
   Scenario: master lives on Store (NFS) and is written from login nodes; nothing stops a cron ingest on login01 and a manual checksums on login02 writing at once, relying entirely on NFS fcntl locking (the classic SQLite-on-NFS corruption case). The README's "read-only copy synced to Scratch" (README.md:139-144, 362-366) has no tool; a plain cp/rsync of a DB mid-commit can produce a torn copy. Recovery is "restore from Store backups" with no documented procedure. Positive: journal_mode is the default rollback `delete`, not WAL [verified], so the NFS/WAL trap is avoided.
   Where: seqledger/db.py:79-84 (no busy_timeout beyond Python's 5 s default, no lock, no pragmas); no backup/vacuum/integrity_check code in the package (grep).
   Fix: add `seqledger backup DEST` (sqlite3 `Connection.backup` or `VACUUM INTO`), `seqledger check` (integrity_check + foreign_key_check), and `seqledger publish` (backup to a temp name then atomic rename on Scratch for the GUI); take an O_EXCL lockfile next to the DB (host+pid, stale detection) in every writer command.

8. **high** - `--prune-projects` deletes disk-only projects when the metadata side is fine but the seqdata mount is an empty mountpoint; the guard messages are wrong and there is no CLI override.
   Scenario: Store not mounted, `/store/.../raw_sequence_data` exists as an empty dir; `ingest --prune --prune-projects --yes` keeps mapfile projects (union with metadata) but deletes `LAB-X_nomap` (a folder with no mapfile) and cascades its file rows with md5/integrity history, because 1 of 7 is under the 50% cap. [verified] Separately, when the cap does trip (4 of 7 gone), the CLI prints "no projects discovered under the roots (empty / unmounted / wrong path?)", which is false, and the suggested override `force=True` is not reachable from the CLI. [verified] Cap is relative per run, so on 400 projects 199 can go in one run.
   Where: seqledger/ingest.py:636-659 (isdir only, 50% cap at 653); seqledger/cli.py:253-255 (ignores pr["reason"]), 720-724 (no --force).
   Fix: require a sentinel (e.g. a `.seqledger_root` marker file or non-empty listdir) per root, never prune a project that has files with md5/integrity history unless `--force`, print `pr["reason"]`, and add an absolute cap (e.g. max N projects) plus `--dry-run`.

9. **medium** - `--prune` on a truncated mapfile deletes most of a project's samples, with no cap and no preview.
   Scenario: someone saves a mapfile with only the first row; `ingest --prune --yes` deletes S2 and S3 and cascades their file rows (md5/integrity history gone). Output says "pruned 2 sample(s) and 0 stale file row(s)" although 4 file rows were deleted by cascade. The y/N prompt is asked before ingest computes what would be deleted. [verified]
   Where: seqledger/ingest.py:467-493 (only the zero-files case is guarded; comment at 484 still says filename is UNIQUE); seqledger/cli.py:129-149, 180 (prompt before preview), 244-245.
   Fix: compute the prune set first, show it, then confirm; refuse when more than X% of a project's samples would go; count cascaded file rows in the message.

10. **medium** - A local `integrity` run holds the write lock across up to 200 file reads; other writers die after 5 s.
    Scenario: integrity over 2 GB FASTQs on Store holds a RESERVED lock for tens of minutes between commits; cron `ingest` or `checksums` fails with "error: database is locked" after exactly 5 s. [verified: ingest exited 1 at 5.05 s]
    Where: seqledger/integrity.py:45, 340-346; seqledger/db.py:81 (default timeout).
    Fix: commit on a time budget (e.g. every 30 s) as well as count, and set `timeout=60` / `PRAGMA busy_timeout` in `connect()`.

11. **medium** - Ingest's "graceful" offline-taxonomy path does not catch the error taxonomy actually raises.
    Scenario: first ingest on a node without internet: taxdump download raises RuntimeError, ingest prints a traceback, exits 1, and skips the data-files check, although metadata was committed. [verified]
    Where: seqledger/taxonomy.py:104 (RuntimeError); seqledger/cli.py:273 (catches OSError/URLError/TarError only), 834.
    Fix: catch RuntimeError there (or raise OSError subclass from `_download`), and add RuntimeError to main's one-line handler.

12. **medium** - Config: one advertised key is ignored, several cluster values are hardcoded, and config input is not validated.
    - `backup_location` is ignored: 'pdrive' is hardcoded in seqledger/checksums.py:169, seqledger/query.py:35, seqledger/app/streamlit_app.py:281.
    - "submitted N job(s) to lTIO.sq" ignores io_queue: seqledger/cli.py:501; GUI wait label "max 2 jobs/user": seqledger/gui.py:186; `LTIO_MAX_SLOTS = 6`: seqledger/rclone.py:67; Hydra-specific `-l ioq`, `mres`, `-pe mthread`: seqledger/cli.py:417-419, seqledger/gui.py:102-103; `source ~/.bashrc`: cli.py:427, gui.py:108.
    - `--set io_qeue=typo` is stored silently [verified]: seqledger/cli.py:85-90.
    - Roots are stored as typed (relative `data/seqdata` kept relative [verified]), so cron or `qsub -cwd` from another dir resolves differently: seqledger/cli.py:81-93.
    - No config for batch defaults (slots 4, mem 2, batch-dir), the taxdump URL, or the `_mapfile.csv` suffix.
    Fix: honor `backup_location` in all three places, reject unknown keys in `--set` (allow `--set-unsafe`), absolutize path keys at init-db, and move the SGE resource line and env-activation snippet into config.

13. **medium** - qsub job templates activate conda fragilely and the GUI job can report READY for a dead server.
    Scenario: many site `~/.bashrc` files return early for non-interactive shells, so `conda activate` fails and `python -m seqledger` runs under the system Python; the integrity job then fails with an unhelpful error. For `gui --qsub`, the wait loop falls through after 90 s and writes READY regardless, so the user gets a tunnel to nothing.
    Where: seqledger/cli.py:427-429; seqledger/gui.py:108-109, 125-131.
    Fix: bake the absolute interpreter path (`sys.executable` at submit time) into the script, or use `eval "$(conda shell.bash hook)"` plus `set -e`; only write READY when the port check succeeded, else write FAILED.

14. **medium** - Unpinned GUI dependencies and per-user envs: pandas 3 silently disables the CSV-injection sanitizer.
    Scenario: a colleague creates the env today, gets pandas 3 (string dtype is no longer `object`), and Grab & Go / table CSV exports go out unsanitized; the repo's own test fails here. [verified]
    Where: seqledger/app/streamlit_app.py:509-516; environment.yml (`pandas>=2.0`), requirements.txt, pyproject.toml:23.
    Fix: test `pd.api.types.is_string_dtype` (or object or string) and ship a pinned lock (conda-lock or exact versions) for the GUI env.

15. **medium** - Migrated legacy catalogs keep FK-less tables, so cascades silently stop working.
    Scenario: on a catalog migrated from an old schema, deleting a project (via `--prune-projects`) removes its files (rebuilt table has FK) but leaves its samples orphaned; `PRAGMA foreign_key_check` reports nothing because no FK is declared. [verified]
    Where: seqledger/schema.sql:4-6; seqledger/db.py:218-252 (only `files` is rebuilt).
    Fix: rebuild samples/backups/validation_log/data_check_issues to current DDL in the versioned migration, or delete children explicitly in prune.

16. **low** - Ingest against an empty mountpoint rewrites every project row with misleading state.
    Scenario: all projects flip to `missing_seqdata`, `owner_name` is overwritten with NULL, and the stored data-check stays `ok` while the run prints "unchecked". Recoverable by re-ingest, but the GUI is wrong until then. [verified]
    Where: seqledger/ingest.py:250-251 (owner not COALESCEd), seqledger/cli.py:188-192 (isdir only).
    Fix: refuse ingest when the seqdata root is empty but the catalog has files under it, and COALESCE owner columns.

17. **low** - `validate` says the disk check was skipped when it actually ran.
    Scenario: no `--seqdata-root` and no configured root: it prints "(data-files check skipped ...)" but still scans each project's stored root and persists new issues. [verified]
    Where: seqledger/cli.py:371-372 vs seqledger/validate.py:283.
    Fix: print the message only when no project had a usable root.

18. **low** - `--only-unchecked` only means "never checked".
    Files with status `unchecked` (not found) or `gzip_error` do not qualify, so a project whose files were missing during the last run is not resubmitted.
    Where: seqledger/cli.py:107-111.
    Fix: include `integrity_status IN ('unchecked')` or rename the flag to `--only-never-checked`.

19. **low** - Taxonomy review files are clobbered and "pinned" is not true.
    Every ingest that resolves new taxa overwrites `<db dir>/taxonomy_review.csv`, losing an in-progress hand edit; the review CSV prefills `confirmed_taxid` for fuzzy rows, so `apply` confirms everything not edited; the taxdump URL is "latest", not the "pinned" dump the README promises (README.md:299).
    Where: seqledger/cli.py:278-280; seqledger/taxonomy.py:602-615, 20.
    Fix: write dated review files, leave `confirmed_taxid` blank for non-exact rows, and record the dump's Last-Modified/md5 in TAXDUMP_VERSION.txt.

20. **low** - Ingest scale: fine, but it does redundant NFS work and rewrites everything.
    Measured locally: 300 projects / 15k samples / 30k files: 7.5 s first ingest, 5.5 s no-op re-ingest; 601 commits took 2.6 s of 6.2 s, and `_known_uniq_ids` (full samples scan per project, O(P x S)) took 1.8 s. On NFS each commit costs several round trips, and each run walks every project tree twice per extension in ingest and again in the post-ingest validate, plus one stat per file, with no change detection.
    Where: seqledger/ingest.py:139-168, 230-234, 371, 495, 532; seqledger/cli.py:297 (validates all projects, not just ingested).
    Fix: one `os.walk`/scandir pass per project with suffix test, load known UniqIDs once per run, and skip projects whose mapfile mtime and dir listing are unchanged.

21. **low** - Ops hygiene gaps.
    No log file or timestamps; `\r` progress lines pollute qsub logs (seqledger/integrity.py:215-217); checksums prints one WARN per file (tens of thousands of lines); `~/.seqledger_gui/` job/log/ready files are never cleaned (seqledger/gui.py:144-149); `--batch-dir` defaults to cwd-relative `./integrity_batch` (cli.py:756); generated rclone copy jobs always exit 0 even when `rclone copy` fails, and `--progress` floods the job log (seqledger/rclone.py:101-103, 142); `date_ingested` is overwritten on every re-ingest so first-ingest date is lost (seqledger/ingest.py:251, 255); no `--dry-run` on ingest, prune, or checksums.

## (c) Works well

- Batch workers open the master with `mode=ro` + `query_only` and write per-project JSON; only a serial `--collect` writes the DB (seqledger/cli.py:514-522, seqledger/db.py:87-99).
- Atomic checkpoint writes (`os.replace`) and a `.done` marker that stops collect from merging half-finished jobs (seqledger/integrity.py:359-368, 462-466).
- Rollback journal, not WAL, so NFS is not hit by the WAL shm trap; GUI uses `mode=ro`, a 15 s busy_timeout, and retry with a friendly message (seqledger/app/streamlit_app.py:143-198).
- `validate` does all disk scans before taking a write lock and commits per project (seqledger/validate.py:272-316).
- Ingest is idempotent: `init-db` rerun leaves the file byte-identical, and a no-op re-ingest adds 0 rows [verified]; a clean legacy migration preserved file_pk, md5, and direction [verified].
- Prune guard refuses non-TTY runs without `--yes` (exit 1) and refuses missing roots; discovery is the union of both roots, which protects against one side vanishing; empty-mapfile file prune is blocked.
- ENOENT vs transient stat errors are distinguished so a network blip does not downgrade verified files (seqledger/integrity.py:103-118).
- Friendly first-run errors (missing root, missing catalog for `query`, no stray empty DB created).
- Core CLI is stdlib-only, so it runs on any login node.

## (d) Top 5 recommendations

1. Fix the two silent-wrong-answer paths first: scope the checksums "absent" reset to projects present in the listing and abort on the truncation heuristic (#1); store check-time size/mtime for integrity and reset on change (#2).
2. Make batch integrity stale-proof: seed only for unfinished jobs, re-stat seeded files, archive JSON after collect, and guard collect writes by project and date (#3); record open/read OSErrors as `unreadable`, not corrupt (#4).
3. Add DB management commands: `backup` (VACUUM INTO / backup API), `check` (integrity_check + foreign_key_check), `publish` (atomic snapshot for the GUI), plus `PRAGMA user_version` and an explicit, transactional, backup-first `migrate` that write commands require instead of auto-running (#6, #7, #15).
4. Make it cron-safe: meaningful exit codes, timestamped log file option, a cross-host writer lockfile, a 60 s busy_timeout, and time-based commits in integrity (#5, #7, #10).
5. Harden prune: preview-then-confirm, `--dry-run`, per-project and absolute caps, a root sentinel file, accurate refusal messages, a real `--force` flag, and never deleting rows that carry md5/integrity history without it (#8, #9).

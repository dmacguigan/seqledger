# seqledger CLI: output noise and an interactive mode

Reviewer: CLI UX. Runs used the main checkout at devel f831942 (devel later moved
to 82e0894, a GUI-only merge; CLI code unchanged). Nothing in the repo edited.
Builds on 00_SUMMARY.md and 02_novice_user.md; their findings (integrity does not
name the bad file, review CSV overwritten, fuzzy guesses auto-confirmed, gui
fail-fast) are not repeated.

Scratch: /tmp/claude-1000/-home-dmacguig-Documents-GitHub-seqledger/3bcc3cd7-35f2-45f8-8e0b-6b4fa8b86b3c/scratchpad/cli_ux/
- build_demo.py: 3 projects (Gadids OK; Crabs with a truncated gzip; Squid with a
  missing and an unlisted file), md5 listings with 1 mismatch, 1 P-drive gap.
- build_scale.py: 100 projects, 4,900 samples, 10,000 files (5 projects with
  missing files, 2 with no mapfile, P-drive listing missing 10 projects).
- run.sh, runs.sh; out/*.out and out/*.err hold the exact output of every run.
- Taxonomy used an existing taxdump via symlinks (no download).

## A. How complex and noisy is the output?

### A1. Per-command results on the demo catalog (3 projects, 7 samples, 14 files)

| Command | stdout lines | exit | What a first-time user needs | Noise |
|---|---|---|---|---|
| init-db (with roots) | 1 | 0 | "catalog created at X, roots set" | fine; but relative roots are stored as typed |
| init-db --show | 11 | 0 | the config | fine (admin only) |
| ingest (first run) | 25 | 0 | counts, which project has problems, next step | 2 "(using configured ...)" lines, 3 section banners, `\r` progress line, review CSV absolute path printed twice, 2-line "next" hint |
| ingest (re-run, no change) | 22 | 0 | "nothing changed" | same 22 lines as a first run; 3 projects repeat their warnings |
| integrity | 6 | 1 | which file is broken | `\r` counter, "using configured", "0 gzip-error, 0 format-error" columns on OK rows |
| checksums | 5 | 1 | "1 file differs, backups verified for 0 of 3" | per-file WARN lines; "orphan" jargon |
| validate | 12 | 1 | which projects need attention | every project prints 3 lines even when OK; "incomplete (1 uncompared)" |
| taxonomy resolve (nothing new) | 3 | 0 | "nothing new to look up" | tells user to review and apply a CSV that is now empty |
| taxonomy resolve --source both | 10 | 0 | counts + one review file each | two `\r` counters for WoRMS, 4 long absolute paths |
| taxonomy apply | 1 | 0 | fine | none |
| query summary | 4 | 0 | table | headers are column names: data_check_status, n_mismatch, n_uncompared, owner_name |
| query unbacked | 4 | 0 | table | "verified 0" column is always 0 by definition |
| query taxa (empty) | 1 | 0 | nothing, or a message on stderr | "(none)" is printed to stdout, so a pipe gets a fake data row |
| ingest --prune --prune-projects (cron, no --yes) | 2 + 1 stderr | 2 | refusal reason | good; message on stderr |
| ingest --prune --prune-projects --yes | 26 | 0 | what was deleted | fine, but same banners and hints as a plain run |
| integrity --batch --no-submit | 7 | 0 | where scripts went, how to collect | one "wrote" line per project (500 at scale) |
| `seqledger` (no subcommand) | 0 + 4 stderr | 2 | a hint where to start | "the following arguments are required: cmd" |

Notes from the runs:
1. `query samples`, `query backups` and a `prune-projects` subcommand do not exist
   (use `query search`, `query unbacked`, `ingest --prune-projects`). Guessing
   gives a 4-line argparse error; the menu in section B removes the guessing.
2. All `\r` progress counters (integrity.py:223, taxonomy.py:536,
   worms.py:177/275) print even when stdout is a file or a cron log; captured
   output is one long line of carriage-return segments. In a qsub `-o` log of a
   multi-hour integrity run that is one ~36 KB line per hour.
3. Ingest (run 22) printed `WARN: 'G3_R1.fastq.gz' on disk is not referenced in
   metadata` for the Gadids project, then two sections later printed
   `[OK] genohub-101_Gadids: data-files OK`. The two checks define "orphan"
   differently (ingest: not in the CSV; data-files check: not cataloged), so one
   run tells the user both "problem" and "OK" for the same files.
4. Missing-mapfile projects say the same thing twice: a 260-character
   `MAPFILE [missing_mapfile]: ...` line, then `WARN: no mapfile for this project ...`.
5. A plain ingest says nothing about a project that vanished from disk and
   metadata (Squid, run 22). It is only mentioned when `--prune-projects` actually
   deletes it, so there is no preview before the destructive step.
6. Developer wording that reaches users: `pass force=True to override`
   (ingest.py prune refusal; there is no CLI flag for it), `name project_id(s)`
   (integrity collect warning), `MAPFILE [missing_mapfile]`, `orphan`,
   `uncompared`, `format-error`, `read-count parity`, and the raw column names in
   every `query` header.
7. Hints that repeat every run: "(using configured seqdata-root: ...)" on ingest,
   integrity and validate; the 3-line "next: run the integrity check" block on
   every ingest, even a no-op re-run; "review + edit ... then apply" on every
   resolve, even with 0 results. The `integrity --batch` hint ("or on Hydra:") is
   shown to anyone, including a biologist who should never run it.

### A2. What scales badly (100 projects / 10,000 files measured, 50,000 projected)

| Command | 10k-file run | Per-run cost at ~500 projects / 50k files | Cause |
|---|---|---|---|
| ingest (no changes) | 230 lines, 188 are `[OK] project` | ~1,100 lines every cron run | one line per project in two sections (ingest + data-files) |
| ingest (problems) | 3 lines per missing file | one line per missing or unlisted file | row-level findings printed in full |
| checksums | 1,001 lines (985 "missing from P-drive", 15 "absent from both") | one line per affected file; a P-drive listing missing 10% = 5,000 lines | checksums.py:115-160 appends one warning per file; cli.py:356 prints them all |
| checksums order | random | random | warnings come from iterating a `set`; two runs on the same data print in different order, so diffing cron logs is useless |
| validate | 316 lines | ~1,500+ | 3 lines per project even when OK, plus one per bad file |
| integrity | 103 lines (95 OK) | ~500 lines | one line per project; still does not name bad files |
| query summary | 101 rows | 500 rows | expected; it is data, keep it |

### A3. Proposed convention

One rule for every non-query command:
- **Default (no flag): one summary block.** Counts, then a "Problems" list capped
  at 10 lines (`... and 37 more; add -v to list all`), then one "Next:" line only
  when there is a next step for this user. No section banners, no OK rows, no
  "(using configured ...)" notes, no progress on a non-terminal.
- **-v: per-project detail.** One line per project including OK ones, the
  "using configured root" note, and the full problems list grouped by project.
- **-vv: per-file detail.** Every per-file warning (missing, unlisted, md5
  mismatch, absent from listings, gzip error), sorted by path.

Implementation sketch (small, cli.py only plus the progress gates):
- `p.add_argument("-v", "--verbose", action="count", default=0)` on the top
  parser and on the `dbp` parent (same SUPPRESS trick already used for `--db`) so
  `-v` works before or after the subcommand.
- Core modules already return the per-file data (findings lists, `warnings`,
  per-project dicts); only the printing in cli.py changes. One helper,
  `_problems(lines, verbose, cap=10)`, prints the capped list.
- `sorted(warnings)` in cmd_checksums; group to counts per kind at default.
- Progress: pass `progress=sys.stderr.isatty()` and write the counter to stderr.
  On a non-terminal print nothing (or one line per minute at -v).
- No `--log` flag. Cron runs `seqledger -vv ingest >> ingest.log 2>&1`; the exit
  code already says whether to alert. Per-file detail is also in the DB and GUI.

stdout vs stderr:
- `query` stdout must be only the TSV: move "(none)" to stderr (header-only or
  empty stdout), so `seqledger query unbacked | wc -l` and `cut` work.
- Progress counters, "Downloading NCBI taxdump", WoRMS "querying" lines, and the
  "(using configured ...)" note go to stderr in every command.
- Summary blocks and problems stay on stdout (they are the result).
- Already right: missing-catalog message, prune refusal, argparse errors.

### A4. Before and after for the three worst commands

**1. ingest** (demo, first run). Before, 25 lines:
```
(using configured seqdata-root: raw_sequence_data)
(using configured metadata-root: raw_sequence_data)
== ingest ==
[OK] genohub-101_Gadids
[OK] genohub-102_Crabs
[WARN] genohub-103_Squid
    WARN: row 3: R2 'S2_R2.fastq.gz' not found on disk
    WARN: 'S9_R1.fastq.gz' on disk is not referenced in metadata
ingested 3 project(s), 0 rejected (FAIL); 7 new sample(s), 0 changed, 14 new file(s)

== taxonomy resolve ==
  resolved 7/7 taxa
resolved 7 taxa (1 fuzzy/unresolved)
review + edit confirmed_taxid in: /long/abs/path/demo/taxonomy_review.csv
then: seqledger --db catalog.db taxonomy apply --review /long/abs/path/demo/taxonomy_review.csv

== data-files check ==
[OK] genohub-101_Gadids: data-files OK, checksum incomplete (6 uncompared)
[OK] genohub-102_Crabs: data-files OK, checksum incomplete (4 uncompared)
[WARN] genohub-103_Squid: data-files 1 missing, 1 orphan, checksum incomplete (4 uncompared)

ingest complete.
next: run the integrity check when ready (slow; reads every byte) --
  seqledger --db catalog.db integrity
  or on Hydra: seqledger --db catalog.db integrity --batch
```

After, 8 lines:
```
Ingest: 3 projects (2 OK, 1 with problems, 0 rejected)
  7 new samples, 0 changed, 14 new files
  Species names: 7 looked up, 1 needs a check -> taxonomy_review.csv (next to the catalog)
Problems:
  genohub-103_Squid: 1 file in mapfile not on disk, 1 file on disk not in mapfile
Backups not yet verified: 3 projects
Next: seqledger integrity   (checks files for damage; slow)
More detail: add -v (per project) or -vv (per file)
```

A no-change re-run (today 22 lines) becomes `Ingest: 3 projects, nothing new or
changed.` plus Problems; the 100-project run (230 lines) becomes about 12 lines.

**2. checksums** (10,000-file run). Before, 1,001 lines in random order:
```
updated md5 for 9985 file(s) across 100 project(s)
    WARN: genohub-007_P/genohub-007_P_S1_R1.fastq.gz: missing from P-drive md5 listing
    WARN: genohub-006_P/genohub-006_P_S45_R1.fastq.gz: missing from P-drive md5 listing
    ... 983 more like this, shuffled ...
    WARN: cataloged file 'genohub-004_P/genohub-004_P_S2_R2.fastq.gz' absent from both md5 listings (backup unverified)
```

After, 7 lines:
```
Backups: compared 9,985 files in 100 projects
  Verified: 90 projects
  985 files missing from the P-drive listing (10 projects: genohub-000_P, genohub-001_P, ... +7 more)
  15 cataloged files in neither listing (5 projects)
  0 files differ between Store and P-drive
Not verified: 10 projects. List them: seqledger query unbacked
More detail: add -v (per project) or -vv (every file)
```

Demo: `1 file differs between Store and P-drive: genohub-101_Gadids/G3_R2.fastq.gz`
(mismatches are rare and serious, so up to 10 are always named at default).

**3. validate** (10,000-file run 316 lines; demo 12). Before (demo):
```
(using configured seqdata-root: raw_sequence_data)
genohub-101_Gadids
    data-files: OK
    checksum:   1 mismatch
genohub-102_Crabs
    data-files: OK
    checksum:   incomplete (1 uncompared)
genohub-103_Squid
    data-files: 1 missing, 1 orphan
    checksum:   incomplete (1 uncompared)
    WARN: mapfile file 'genohub-103_Squid/S2_R2.fastq.gz' not found on disk
    WARN: disk file 'genohub-103_Squid/S9_R1.fastq.gz' not referenced in mapfile
```

After:
```
Checked 3 projects: 0 fully OK
Problems:
  genohub-101_Gadids: 1 file's backup copy differs
  genohub-103_Squid: 1 file in mapfile not on disk, 1 file on disk not in mapfile
Backups not yet verified: 2 projects (genohub-102_Crabs, genohub-103_Squid)
More detail: add -v (per project) or -vv (per file)
```

Integrity is fourth: 103 lines at 10k files, and the one thing a user needs (the
broken file's path) is missing. With the convention, default shows
`1 damaged file: genohub-102_Crabs/C2_R2.fastq.gz (gzip error)` and drops OK rows.

Wording swaps (one place each, cli.py labels): orphan -> "on disk, not in
mapfile"; uncompared / incomplete -> "backup not yet verified"; format-error ->
"bad FASTQ line count"; read-count parity -> "R1 and R2 read counts differ";
`force=True` -> drop the hint (no flag exists) or name a real flag; query headers
keep column names (they are for scripts) but `query -h` epilog explains them.

## B. Interactive mode

### B1. Who uses which commands

Biologists: `query search`, `query uniq-id`, `query summary`, `gui --qsub`.
Data managers: `ingest` (cron), `integrity --batch` / `--collect`, `checksums`
(rclone listings from two machines), `taxonomy resolve` / `apply`, rarely
`init-db` and `ingest --prune --prune-projects`. So browse and GUI go first in
the menu; the flag CLI stays exactly as is for scripts and cron.

### B2. Entry point and TTY rule

`seqledger` with no subcommand (optionally `seqledger --db PATH`) opens the menu
only when stdin and stdout are both terminals. Otherwise behaviour is today's:
usage on stderr, exit 2. No separate `menu` subcommand (one way in is enough).

cli.py change (about 10 lines):
```python
sub = p.add_subparsers(dest="cmd")          # was required=True
...
parser = build_parser(); args = parser.parse_args(argv)
if args.cmd is None:
    if sys.stdin.isatty() and sys.stdout.isatty():
        from seqledger import interactive
        return interactive.run(args.db)
    parser.error("choose a command (or run 'seqledger' in a terminal for a menu)")
```

### B3. Proposed menu text

```
seqledger  -  catalog: /store/.../oceandna_catalog.db  (512 projects, 5,031 samples)

  Look things up
    1  Search samples (sample ID, species, or UniqID)
    2  Project overview
    3  Open the browse GUI

  Data manager tasks
    4  Add or update projects from the mapfiles (ingest)
    5  Check files for damage (integrity)
    6  Verify backups from md5 listings (checksums)
    7  Look up species names (taxonomy)

    c  Use a different catalog, or create one
    q  Quit
Choose:
```

Every action first prints `Same thing without the menu:` and the command, for
example `seqledger --db /store/.../oceandna_catalog.db ingest`, then runs it
with its normal output and returns to the menu after "Press Enter".

Prompts per item (defaults in brackets come from the catalog config or the
environment; Enter accepts):
1. `Search for: ` -> `query search TERM`.
2. Sub-list: overview (`query summary`), exact UniqID (`query uniq-id`),
   not backed up (`query unbacked`), backup copies differ (`query mismatches`),
   names needing review (`query taxa`).
3. If `qsub` is on PATH: `gui --qsub` (never serve from a login node); else
   `gui` locally and say "press Ctrl-C to stop the GUI and return to the menu".
4. Shows `Sequence data folder [configured]` and `Mapfile folder [configured]`;
   Enter keeps them (no flags emitted). If the taxdump is not yet downloaded:
   `Look up new species names now? First time downloads about 80 MB and uses
   about 1.4 GB next to the catalog. [y/N]` (N adds `--skip-taxonomy`). Then runs
   plain `ingest` (never deletes). Afterwards, only if that run reported
   samples "kept" or vanished projects: `Delete the N samples and M projects listed
   above as no longer in the mapfiles or on disk? Type DELETE to confirm:` then
   runs `ingest --prune --prune-projects --yes`.
5. `Run on this node or submit to the I/O queue? [q/n]` (default q if qsub
   exists) -> `integrity --batch --only-unchecked` or `integrity`. If an
   `integrity_batch/results` folder exists: `Merge finished batch results first?
   [Y/n]` -> `integrity --collect DIR`.
6. Prints the two rclone md5sum commands from the README as a reminder, then
   `Store listing file:` and `P-drive listing file:` (must exist), `Only one
   project? [all]` -> `checksums --store A --pdrive B [--project P]`.
7. `taxonomy resolve` (same download warning as item 4). Apply is NOT in the
   menu in v1: the menu prints the apply command and tells the user to edit the
   review CSV first (see risk R4).
c. `Catalog path [current]:` If missing: `No catalog there.
   Create a new one? [y/N]` -> prompts for name, sequence data folder, mapfile
   folder (converted to absolute paths) -> `init-db --name ... --seqdata-root
   /abs ... --metadata-root /abs`. Biologists answer N and are told to ask their
   data manager for the path.

Remembering the catalog: config lives inside the catalog, so it cannot store
the catalog's own path. Default is `--db`, else `$SEQLEDGER_DB`. After a pick the
menu prints once: `Tip: add to ~/.bashrc: export SEQLEDGER_DB=/abs/path.db`. No
new dotfile.

### B4. Calling the commands: argv, not Namespace

Recommendation: every action builds an argv list and calls `cli.main` logic via `cli.run(argv)`.
Reasons: the real parser fills every default (cmd_integrity reads 12 attributes,
cmd_ingest 9), `_require_db` and the one-line error handling in `main` run as in
the CLI, and `shlex.join(argv)` IS the "same thing without the menu" line, so the
printed command can never drift from what ran.

Direct `cmd_*(Namespace(...))` calls would work for cmd_query, cmd_validate,
cmd_checksums, cmd_taxonomy, cmd_init_db (uses getattr) and cmd_gui (3 to 8
attributes each). cmd_ingest (9 attributes, reads sys.stdin.isatty() itself) and
cmd_integrity (12) are fragile that way.

None needs refactoring if argv is used. Three small changes are needed:
1. cli.main: optional subcommand + TTY rule (B2), about 10 lines.
2. Prune preview: `prune_missing_projects(..., dry_run=False)` returns `gone`
   without deleting (3 lines), and plain `cmd_ingest` prints `N project(s) gone
   from disk and mapfiles: X (kept; --prune-projects removes them)` (4 lines).
   This also fixes A1 note 5 for CLI and cron users. So the menu knows whether
   to offer the delete step, `cmd_ingest` stores the two counts on `args`
   (`args.prunable = (n_kept_samples, n_gone_projects)`), and a 5-line
   `cli.run(argv) -> (rc, args)` wrapper (which `main` also uses) hands them back.
3. SystemExit handling in interactive: `sys.exit("msg")` only prints when the
   exception reaches the interpreter. The menu catches SystemExit, so it must
   print `e.code` when it is a string, else the user sees nothing.

### B5. Module layout (seqledger/interactive.py, about 200 lines, stdlib only)

`run(db)` main loop (catches KeyboardInterrupt/EOFError, returns 0); `_header`
(path + project/sample counts via connect_ro); `_ask(prompt, default)`, `_yes`;
`_run(db, argv)` (echo command, call cli.run, print SystemExit text, wait for
Enter); `_cfg(db, key)`; `_taxdump_ok(db)`; then one 10-25 line function per
menu item that only builds argv. No classes, colours, curses, or framework.
Imports: os, shlex, shutil, sys, seqledger.cli, seqledger.db.

### B6. Tests (tests/test_interactive.py, about 90 lines)

- Feed answers: `it = iter([...]); monkeypatch.setattr("builtins.input",
  lambda prompt="": next(it))`; call `interactive.run(db)` directly.
- Non-TTY: `subprocess.run([sys.executable, "-m", "seqledger"], stdin=DEVNULL)`
  -> exit 2, usage on stderr, empty stdout.
- Echo: overview -> stdout has `seqledger --db DB query summary` and the table.
- Prune: dropped-sample fixture; "n" keeps the sample, "DELETE" removes it.
- Missing catalog + "n" at create -> no file created.
- EOF and Ctrl-C return 0; no taxdump + "n" -> argv has `--skip-taxonomy`.

### B7. Rough size

interactive.py ~200; cli.py ~20 (optional subcommand, cli.run, prune notice);
ingest.py ~3 (dry_run); tests ~90; README ~6 ("Not sure where to start? Run
`seqledger`"). Total about 320 lines, 200 of them in one new file.

### B8. Risks

- R1 Catalog guard: `main` refuses a missing catalog for all but init-db; the
  menu always passes `--db`, and "create" defaults to N so a typo cannot make one.
- R2 Relative roots: init-db stores roots as typed; the menu must abspath every
  path it puts in argv.
- R3 Prune: only after a non-destructive run shows the list, and only with a
  typed `DELETE`, then `--yes`. Never chain prune into the default ingest path.
  The >50% blast-radius refusal still applies; its message needs the
  `force=True` wording fixed (A1 note 6).
- R4 Taxonomy apply: fix/taxonomy-review is not on devel yet; the review CSV
  still pre-fills confirmed_taxid on every row. Keep apply out of the menu.
- R5 Taxonomy download: first resolve (and first ingest with new taxa) pulls
  ~80 MB and writes ~1.4 GB beside the catalog; on Store that may be the shared
  master folder. Always ask first when the index is absent.
- R6 GUI: local `gui` blocks; gui.launch already catches Ctrl-C, and the menu
  must also catch it around `_run` so one Ctrl-C returns to the menu.
- R7 Locks: a menu ingest during a cron ingest waits up to 60 s, then shows
  "database is locked". No special handling.
- R8 Scope creep: no saved preferences, history, colours, or init-db wizard
  beyond name and two roots. Admin flags stay CLI-only.

## Recommended order of work

1. fix/output-trim (cli.py mostly): progress only on a terminal, to stderr;
   "(none)" and notes to stderr; sort checksums warnings; dedupe missing-mapfile
   text; drop "next"/"review" hints when nothing changed; fix `force=True`
   wording. No new flags. Small, testable, helps cron logs now.
2. feat/verbosity: `-v/-vv`, summary block per command, capped Problems list,
   integrity names damaged files. Update tests that assert on output text.
3. fix/prune-preview: `dry_run` for vanished projects + notice in plain ingest.
   Needed by the menu, useful on its own.
4. feat/interactive: seqledger/interactive.py + optional subcommand in main,
   apply left out until fix/taxonomy-review is merged.
5. README: one paragraph pointing novices at `seqledger` (menu) and the
   `export SEQLEDGER_DB` tip.

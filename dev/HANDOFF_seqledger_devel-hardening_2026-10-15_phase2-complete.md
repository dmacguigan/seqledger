# Handoff: seqledger review-driven hardening, Phases 1, 2, and 2b complete

- **Branch:** `devel`, cut from `main` at `f3174b6`.
- **Tip:** `c0d99dd` (Phase 2b merged 2026-10-16). **Not pushed. No PR.**
- **Tests:** `python -m pytest -q` gives 311 passed, 1 skipped (the skip needs plotly). The base miniforge3 `python` on this machine has every dependency; there is no `seqledger` conda env here.
- **Date:** 2026-10-15
- **Status:** Phases 1, 2, and 2b are merged. Phase 3 (ponytail simplification) is next, then Phase 4 (docs).

## How work is organised

- Workflow rules live in `CLAUDE.md` at the repo root: `devel` is the staging branch, every change goes on a working branch off `devel` and is merged with `git merge --no-ff`, only `devel` is ever pushed and only when the maintainer asks. A PreToolUse hook (`.claude/hooks/git_guard.sh`) blocks pushes of other branches and commits on `main` unless `ALLOW_MAIN=1`.
- Maintainer rules: no Claude attribution in commits or PRs, brief commit messages, ASCII only in code, no em dashes, minimal comments, narrow scope.
- Agent pattern used so far and worth keeping: reviewers and implementers run on Opus, the main session only plans, briefs, merges, and resolves conflicts. Each implementer gets its own branch off `devel` in an isolated worktree, a brief with file:line pointers copied from the review reports, and a rule to add tests and CHANGELOG bullets. Independent branches run in parallel; the main session merges them one by one and reruns the suite after each. CHANGELOG `[Unreleased]` conflicts happen on every merge and are resolved by keeping both sides.

## Review reports (all in `docs/review/2026-10-15/`)

| File | Content |
|---|---|
| `00_SUMMARY.md` | roll-up of all reviews and the phased fix plan |
| `01_ponytail_audit.md` | 52 simplification findings, ranked; drives Phase 3 |
| `02_novice_user.md`, `02_data_manager.md`, `02_db_security.md`, `02_documentation.md`, `02_app_ux.md`, `02_correctness.md` | persona reviews with repro scripts |
| `03_cli_output_and_interactive.md` | CLI output volume audit and the interactive-mode design; drives Phase 2b |

Repro and PoC scripts from the reviews are in the session scratchpad only (not in the repo).

## What landed

### Phase 1 (seven branches)

- CSV sanitizer works on pandas 3 (`is_string_dtype`); the one failing test passes. MitoPilot map export is deliberately not sanitized.
- Shell quoting for every config value and filename in generated qsub and rclone scripts; newlines and NUL rejected at `--set` and ingest; `SCRIPT_SAFE_RE` allowlist in `db.py`.
- Integrity: re-ingest with a new size clears stale integrity and md5 results; batch resume reuses JSON only for unfinished jobs; collect is scoped by project and never overwrites newer; unreadable files report `unchecked`.
- Checksums: per-project listing resets only listed projects; `--full` for the old catalog-wide reset; truncated listing aborts unless `--force`; md5 and path normalisation.
- Ingest churn: missing or broken mapfile keeps sample links; moved file keeps its row; prune by rel_path; more than 50 percent sample prune refused unless `force=True` (no CLI flag yet).
- Exit codes and catalog guard: every command except `init-db` refuses a missing catalog; `init-db --show` is read-only; `--db` works after the subcommand; exit 1 on failures, 2 on refusals; `RuntimeError` from the taxonomy step is caught.
- DB and GUI hardening: GUI binds 127.0.0.1 always; new catalogs 0600; read-only URI escapes the path; writer waits 60 s; integrity commits every 30 s; `gui --qsub` reports FAILED instead of a dead tunnel; tunnel command `ssh -N -L <port>:127.0.0.1:<port> -J <user>@<login> <user>@<node>`.

### Phase 2 (four branches)

- `fix/taxonomy-review`: review CSV pre-fills `confirmed_taxid` (and `confirmed_aphia_id`) only for exact matches; apply reads BOM and cp1252 files through `ingest._read_text` and refuses a wrong-type file; review files get a date suffix instead of being overwritten, and none is written when nothing resolved; WoRMS-first taxa are now NCBI-resolved (`date_resolved IS NULL` test in `taxonomy.py`, `cli.py`, and `COALESCE` in `query.py`); genus homonym guard on the fuzzy path; `--redo` docs corrected.
- `fix/gui-selection`: Samples, Projects, and Files tables keyed on the filtered index plus an out-of-range guard; `_REQUIRED_SCHEMA` and `_missing_schema()` startup check with a friendly message; NaN `metadata_detail` fallback; WoRMS and NCBI legend dicts match stored values; Samples table shows readable match labels; plotly-missing and empty-catalog messages.
- `fix/grab-and-go`: MitoPilot map writes `rel_path` (MitoPilot joins `data_path + "/" + R1` verbatim, verified in its nextflow and R code), so `data_path = DEST` works with the copied tree; `issues()` gains `n_lane_split` and `lane_split_ids`; `load_samples` lists every lane file with `GROUP_CONCAT`; `files_ok` column with an "Exclude problem files" checkbox on by default; script preview of 20 files with the full script only in the download; timestamped job and log names; `--progress` dropped; job exits non-zero on any failed copy.
- `fix/phase2-misc`: `backup_location` honored in `checksums.py`, `query.py`, and the GUI; Ctrl-C in local integrity cancels queued files and commits; validate persists `unchecked` when a project folder is gone (root unmounted still skipped) and counts files with no linked sample; zero-length final read no longer a format error; `gui --qsub` default 6 GB; `~/.seqledger_gui` files older than 7 days deleted on launch.

## Maintainer decisions recorded

- Only one live catalog exists and it is current, so legacy migrations (`_MIGRATIONS` in `db.py`), the role-to-direction rename, the files rebuild, and the GUI `_table_columns` shims can be deleted in Phase 3.
- The two-column manual map file (`read_map_file`, `ingest_map_file`, positional `map_file` arg) is unused and can be removed.
- GUI binds 127.0.0.1 always (done).
- Keep Treemap drill-down, the regex search toggle, and the sidebar search. Drop most `init-db` named flags; keep `--set` and the two root flags.
- Keep the "Claude (Anthropic)" author entry in `pyproject.toml`.
- CLI improvement plan (Phase 2b below) approved as proposed.

### Phase 2b (three branches, merged 2026-10-16)

- `fix/prune-preview`: plain `ingest` lists projects gone from both disk and the mapfile folder (`prune_missing_projects(..., dry_run=True)`); `cmd_ingest` sets `args.prunable = (n_kept_samples, n_gone_projects)`; `cli.run(argv) -> (rc, args)` wrapper, `main` returns `run(argv)[0]`.
- `feat/cli-output`: progress counters only when stderr is a terminal, and on stderr; "(none)" and notes on stderr so `query` stdout is pure TSV; checksums warnings sorted; `-v`/`-vv` (count flag on the top parser and the `dbp` parent, SUPPRESS trick); every non-query command prints one summary block with a Problems list capped at 10 via `_problems()`, a "Next:" line only when there is one, and "More detail: add -v" only when something was hidden; integrity names damaged files (`aggregate_from_db` adds `bad_files`); plain-language labels (orphan, uncompared, format-error, parity) and the force=True refusal reworded; `query -h` epilog explains the column names. `integrity --collect` and `--batch` still print one line per project (deliberate).
- `feat/interactive`: `seqledger/interactive.py` (240 lines, stdlib only). `seqledger` with no subcommand on a TTY opens the menu; off a TTY it prints usage and exits 2. Each action echoes "Same thing without the menu:" with the real command and runs it through `cli.run`. Prune only after a plain ingest and a typed DELETE. Taxonomy apply is not in the menu (prints the command instead). README paragraph points novices at the menu and `export SEQLEDGER_DB`.

## Done: Phase 2b, CLI output and interactive mode (for reference, the original plan)

Design and evidence are in `03_cli_output_and_interactive.md`. Four or five small branches in this order:

1. `fix/output-trim` (`cli.py` mostly): progress counters only on a terminal and to stderr; "(none)" and notes to stderr so `query` stdout stays pipeable; sort checksums warnings (they come from a set and print in random order); dedupe missing-mapfile text; drop "next" and "review" hints when nothing changed; fix the "pass force=True" wording. No new flags.
2. `feat/verbosity`: `-v` and `-vv` (count action on the top parser and the `dbp` parent, same SUPPRESS trick as `--db`); default output is one summary block per command with a Problems list capped at 10 and one "Next:" line; `-v` per-project lines; `-vv` per-file lines; integrity names the damaged files. Update tests that assert on output text.
3. `fix/prune-preview`: dry-run listing of vanished projects and a notice in plain ingest. Needed by the menu.
4. `feat/interactive`: new `seqledger/interactive.py` (about 200 lines, stdlib `input()` only). `seqledger` with no subcommand on a TTY opens the menu; without a TTY it prints usage and exits 2. Menu text is in section B3 of the report: "Look things up" (search samples, project overview, open GUI) and "Data manager tasks" (ingest, integrity, checksums, taxonomy), plus "use a different catalog, or create one" and quit. Every action prints "Same thing without the menu:" with the real command, then runs it through the existing CLI via argv (no `cmd_*` refactor). Risks in B8: always pass `--db`, abspath every path, prune only after a preview and a typed DELETE, ask before the taxonomy download, catch Ctrl-C around the run, keep admin flags CLI-only.
5. README paragraph pointing novices at `seqledger` (menu) and the `export SEQLEDGER_DB` tip.

## Then: Phase 3, ponytail simplification

From `01_ponytail_audit.md` and `00_SUMMARY.md`:

- `chore/ponytail-safe`: mechanical items 3, 4, 6, 13, 14, 15, 18, 31 (no behaviour change).
- `chore/ponytail-legacy`: delete the two-column map file path (item 5), legacy migrations and GUI schema shims (items 1, 8; move `idx_files_mismatch` into `schema.sql`), most `init-db` flags (item 10), test dedup (items 45 to 49), `requirements.txt` (item 50). Keep Treemap, regex toggle, and sidebar search.
- Do Phase 3 after Phase 2b so the deletions do not collide with the CLI work.

## Then: Phase 4, docs

- Split README into a landing page plus `docs/user-guide.md`, `docs/admin-guide.md`, reference pages (cli, config, mapfile, statuses, data-model), and a glossary, per `02_documentation.md` section (c). Fix the OWNER clone URL, ellipses in commands, the status table, and add a "Getting your FASTQs" walkthrough.
- CHANGELOG housekeeping (remove the "driven by an adversarial review" note, add compare links), `--version`, `CITATION.cff`, `CONTRIBUTING.md`, non-ASCII cleanup in README and CONTRIBUTORS.md.

## Small leftovers not yet assigned

- No CLI `--force` for the ingest prune cap (the refusal text is now plain language; a flag was deliberately not added).
- Integrity batch JSON is not archived after collect.
- `log_path` and `job_name` config values with spaces now raise at `--set` (by design of the allowlist; document it).
- Non-ASCII characters remain in UI strings in `seqledger/app/streamlit_app.py` and `seqledger/gui.py` (core modules are clean).
- The WoRMS fuzzy step 1 passes all capitalized words including the genus as homonym context, which may stop context from settling a homonym there (noted by the taxonomy implementer, not changed).
- Old hand-confirmed taxa rows with no `date_resolved` now count as "new" and get re-resolved but not overwritten.

## Resuming

1. `git switch devel`, confirm tip `c0d99dd`, run `python -m pytest -q` (base python works).
2. Start Phase 3 (`chore/ponytail-safe`, then `chore/ponytail-legacy`) from `01_ponytail_audit.md`; the interactive menu now imports `cli.run`, `cli._default_taxdir`, `taxonomy._index_path`, and `db.get_config`, so keep those names when simplifying.
3. Then Phase 4.
4. Release only when the maintainer decides: bump `version` in `pyproject.toml`, turn `[Unreleased]` into a dated heading, PR `devel` to `main`, then merge `main` back into `devel`.

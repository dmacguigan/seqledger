# seqledger review, 2026-10-15 (devel @ cbd7759)

Seven independent read-only reviews: a ponytail (over-engineering) audit, then
six reviewers with different expertise and skill levels. Each wrote its own
report in this folder. This file is the roll-up and the proposed fix order.

| Report | Reviewer persona | Headline |
|---|---|---|
| 01_ponytail_audit.md | lazy senior dev | 52 cuts, about -825 lines; three big ones need "every live catalog has run init-db on current code" confirmed first |
| 02_novice_user.md | wet-lab biologist | GUI works once running, but README never explains how to actually get the files; gui and typo'd --db paths fail silently |
| 02_data_manager.md | HPC data manager | checksums without --project wipes other projects' verification; integrity re-reports truncated files as OK; not cron-safe |
| 02_db_security.md | appsec engineer | two confirmed shell-injection paths (config values, mapfile filenames); GUI on 0.0.0.0 with no auth; DB 0644; CSV sanitizer dead on pandas 3 |
| 02_documentation.md | technical writer | about 25 README/code mismatches; taxonomy review docs cause fuzzy guesses to be "confirmed"; split README into user + admin guides |
| 02_app_ux.md | Streamlit product designer | row selection breaks after filtering (crash or wrong row); MitoPilot map + copy job disagree on paths; old catalogs crash on first screen |
| 02_correctness.md | senior Python engineer | 1 critical, 5 high, 12 medium, 15 low, all with repro scripts; failing test is right, code is wrong |

## Findings every reviewer converged on

1. **Integrity trusts a size that ingest overwrites.** Check ok, file gets truncated, routine re-ingest copies the new size in, next integrity skips the file and reports ok. Reproduced by three reviewers. (correctness C1, data_manager 2)
2. **Batch integrity reuses finished results forever.** Old JSON seeds every re-run; --collect re-stamps old statuses over newer ones; file_pk reuse after prune writes one file's status onto another. (correctness H1, data_manager 3, security M2)
3. **CSV formula sanitizer is a no-op on pandas 3.** dtype == object never matches str columns. Cause of the one failing test. One-line fix: pd.api.types.is_string_dtype. (security H3, correctness H4, data_manager 14)
4. **Shell injection via config and mapfile filenames.** conda_env / io_queue / rclone_module and R1/R2 names are pasted unquoted into qsub and rclone scripts. Confirmed with PoCs. (security C1, C2)
5. **checksums without --project resets the whole catalog.** Per-project md5 listings, as the README advises, wipe every other project's md5_match and backups.verified. (data_manager 1)
6. **Re-ingest under churn breaks links.** A briefly missing or broken mapfile unlinks every file from its sample; a moved file forks a duplicate row that prune keeps. (correctness H2, H3)
7. **Taxonomy review flow confirms every fuzzy guess.** CSV pre-fills confirmed_taxid on every row, apply marks all non-blank rows confirmed; review CSV is overwritten by each resolve and ingest; BOM/cp1252 review files silently apply nothing. (correctness M4, M5; docs; novice 14; data_manager 19)
8. **GUI row selection vs filter.** Streamlit keeps selection indices across data changes; every view indexes the new frame with the old row, so filtering after a click crashes or shows the wrong row, including cart removal. (ux 1, correctness M1)
9. **Commands silently create catalogs and exit 0 on failure.** integrity/taxonomy/ingest/init-db --show create an empty DB at a typo'd path; nothing exits non-zero on FAIL, mismatch, or refused prune. (novice 4, data_manager 5, correctness M12)
10. **Dead or misleading config.** backup_location is never read; lTIO.sq, 2-job cap, SGE resource lines, and "source ~/.bashrc" are hard-coded; --set accepts unknown keys. (ponytail 37, all reviewers)
11. **Docs drift.** OWNER placeholder in clone URL, init-db --show migrates, root precedence described wrong, mapfile column order undocumented, a 130-line bash-comment CLI reference, no TOC. (docs, novice)
12. **Non-ASCII in code** (em dashes, arrows, emoji in streamlit_app.py and gui.py) against house rules; pyproject authors lists Claude, against the no-attribution rule. Both pre-date this review; flagged, not changed.

## Proposed fix order (one working branch off devel per line)

Phase 1, correctness and safety (merge first, small and testable):
- fix/csv-sanitizer-pandas3: is_string_dtype; suite goes green.
- fix/shell-quoting: shlex.quote + allowlist for every config/mapfile value in generated scripts; reject newlines at ingest and --set.
- fix/integrity-staleness: store verified size, skip only on match; seed batch JSON only when no .done; collect UPDATE by project_id + newer run_date; reset integrity/md5 state when size changes.
- fix/checksums-scope: reset only projects present in the listing; abort on truncation warning without --force.
- fix/ingest-churn: diskonly ingest never overwrites sample_pk/direction; move rows on subdir change; prune by rel_path.
- fix/exit-codes-and-db-guard: shared "no catalog at" check for every non-init command; non-zero exit on FAIL/mismatch/refusal; print real prune-projects reason; configured queue in message; catch RuntimeError in taxonomy step.
- fix/db-hardening: chmod 0600 on create; bind GUI 127.0.0.1 by default; escape db path in file: URI and set query_only in GUI; busy_timeout on writer.

Phase 2, taxonomy and GUI:
- fix/taxonomy-review: pre-fill confirmed only for exact; utf-8-sig + cp1252 on apply; dated review files; date_resolved IS NULL for scope=new; genus homonym guard.
- fix/gui-selection: drop out-of-range selection indices and key tables on active filters; schema check at startup with friendly message; shorter lock wait.
- fix/grab-and-go: lane-split warning; file-health column; MitoPilot map paths that match the copy layout; script preview not full render.

Phase 3, simplification (ponytail), after phases 1 and 2 land:
- chore/ponytail-safe: items 3, 4, 6, 13, 14, 15, 18, 31 (mechanical, no behaviour change).
- chore/ponytail-legacy: items 1, 5, 8 ONLY after confirming every live catalog has run init-db on current code and nobody uses the two-column map file.
- Product calls, user decides: 2 (treemap), 7 (regex toggle), 10 (init-db flags), 11 (dual search).

Phase 4, docs:
- docs/split-readme: landing README + docs/user-guide.md + docs/admin-guide.md + reference pages (cli, config, mapfile, statuses, data-model) + glossary, per 02_documentation.md section (c). Fix OWNER URL, ellipses in commands, status table, "getting your FASTQs" walkthrough.
- docs/housekeeping: CHANGELOG entries for all of the above, --version flag, CITATION.cff, CONTRIBUTING.md.

Open questions for the maintainer before Phase 3 and some Phase 1 items:
- Has every live catalog (master and GUI copies) been migrated with current code? (gates ponytail 1, 5, 8)
- Is anyone using the manual two-column map file path? (gates ponytail 5)
- Should the GUI keep binding 0.0.0.0 for qsub mode (tunnel to a compute node needs a reachable bind), or bind 127.0.0.1 and tunnel through the node? (security H1)
- Keep or drop the Treemap and the regex search toggle? (ponytail 2, 7)
- pyproject authors entry for Claude: keep or remove?

## Test status at review time

python -m pytest -q: 183 passed, 1 failed (test_csv_safe_df_only_touches_string_columns_and_does_not_mutate). Environment: Python 3.12, pandas 3.0.3, streamlit 1.59.0. plotly is not installed in the base env; the GUI Taxonomy view needs it.

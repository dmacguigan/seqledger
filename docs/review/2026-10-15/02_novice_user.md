# seqledger ease-of-use review: wet-lab novice perspective

Reviewer persona: biologist, can ssh / cd / conda activate / paste commands, no Python or SQL.
Repo: seqledger @ devel (cbd7759). Repo left untouched. All work in
/tmp/claude-1000/-home-dmacguig-Documents-GitHub-seqledger/3bcc3cd7-35f2-45f8-8e0b-6b4fa8b86b3c/scratchpad/novice/
Not on Hydra: no qsub, rclone, or module available.

## (a) Summary verdict

1. Once installed and pointed at the right catalog, the GUI does the job: I found samples, added them to Grab & Go, got full R1/R2 paths, a CSV, a MitoPilot map file, and a copy script.
2. The README "For GUI users" section is short and mostly copy-pasteable, but it stops right before my actual goal: it never tells me how to get the FASTQ files onto my disk (destination, running the job, checking it worked).
3. The rest of the README (about 90 percent of it) is data-manager material full of cluster jargon (Store, P-drive, lTIO, I/O queue, rclone, md5, taxdump) with no glossary; I could not tell what I was allowed to skip.
4. CLI errors are mostly friendly and say what to do next, but several mistakes fail silently or with wrong advice: a typo in the catalog path silently creates a new empty catalog for some commands, and the GUI prints "open this link" even when it failed to start.
5. Command-line lookups (`query`) never show file paths, so the CLI alone cannot answer "where are my FASTQs"; the GUI is the only route, and it requires qsub + an SSH tunnel I would need help with the first time.

## (b) Where I got stuck

1. **blocker: README never explains how to actually grab the files.**
   My task was "find and grab FASTQ files". The user section (README.md:76-83) says Grab & Go will "generate a ready-to-run copy job for their sequence data" and stops. The how-to (what destination to type, that it needs `qsub`, that it uses `rclone`, where the log goes, how to know it finished) is only in the data-manager GUI section (README.md:392-402). In the GUI the destination box shows the placeholder `/pool/public/genomics/<user>/selected_data` (streamlit_app.py:1264-1265) with no explanation of what is a valid place for me.
   Would have helped: a 5-step "Getting your FASTQs" box in the user section: search, add, type destination (example for Hydra), download .job, `qsub file.job`, check `seqledger_rclone_copy.log`. Also mention that the CSV export has full R1/R2 paths if you just want to `cp` a few files yourself.

2. **blocker (for anyone outside the authors' repo): clone URL is a placeholder.**
   README.md:39 and :163: `git clone https://github.com/OWNER/seqledger.git`. I do not know who OWNER is. The real remote is `dmacguigan/seqledger`.
   Would have helped: the real URL.

3. **blocker: GUI says "open this link" even when it did not start.**
   In an env without streamlit (what you get with plain `pip install -e .`, which README.md:167 offers as "CLI only"), `seqledger --db scratch.db gui` printed:
   `On your LOCAL computer, run: ssh -N -L 8767:pop-os:8767 ... Then open http://localhost:8767 in your browser.`
   and then `No module named streamlit`, exit code 0. Because of output buffering the error can appear above the instructions, so I would just follow the instructions and stare at a dead page. gui.py:58-74 prints the tunnel before checking that streamlit exists.
   Would have helped: check for streamlit first and print "GUI packages not installed; run: pip install -e '.[gui]'" and exit non-zero.

4. **confusing: catalog path typo silently creates a new empty catalog (some commands only).**
   `query` and `validate` with a wrong path give a nice message (cli.py:41-45). But `integrity`, `taxonomy`, `ingest`, and `init-db --show` all create an empty .db at the typo path:
   - `seqledger --db ../scrach2.db integrity` -> `no cataloged files to check`, exit 0, and a new scrach2.db appears.
   - `seqledger --db typoT.db taxonomy resolve --source worms` -> `WoRMS: resolved 0 taxa`, new typoT.db.
   - `init-db --show` (sounds read-only) printed the defaults and `initialized schema in ../typo2.db`.
   Now I have a stray catalog and might point the GUI at it later.
   Would have helped: call the same "no catalog at ..." check in every command except `init-db` (and make `--show` read-only).

5. **confusing: the "no catalog" message gives a GUI user the wrong advice.**
   `seqledger --db ../scrach.db query summary` ->
   `no catalog at '../scrach.db'. Create one with: seqledger --db ../scrach.db init-db then ingest your data.`
   As a browse-only user I should never create a catalog; I mistyped the path. Same text when I forget `--db` entirely (`no catalog at 'catalog.db'`), with no hint that `--db` or `SEQLEDGER_DB` is the issue.
   Would have helped: "Check the path (ask your data manager for the catalog path, or set SEQLEDGER_DB). Data managers: create one with ...".

6. **confusing: `--db` must come before the subcommand, and the error does not say so.**
   `seqledger query summary --db ../scratch.db` -> `seqledger: error: unrecognized arguments: --db`. This is the single most natural mistake (every other tool I use takes flags at the end). README shows the right order but never says it matters.
   Would have helped: accept `--db` in either position, or a one-line note in the README.

7. **confusing: GUI launched with a wrong catalog path still starts and prints the tunnel.**
   `seqledger --db ../nope.db gui` printed the tunnel instructions and started the server; only in the browser do you see `Catalog database not found` (streamlit_app.py:1306-1308). Under `--qsub` that means waiting for a job, setting up a tunnel, and only then finding the typo (gui.py launch_qsub never checks the path).
   Would have helped: check the file exists before launching / submitting.

8. **confusing: local `gui` on my laptop tells me to tunnel to Hydra.**
   Running `gui` locally printed `ssh -N -L 8765:pop-os:8765 dmacguig@hydra-login01.si.edu`. On a laptop the right answer is just "open http://localhost:8765"; Streamlit also printed Network and External URLs. README has no "running locally / on a copy" section for users, only the Scratch alternative for data managers (README.md:362-368).
   Would have helped: when not on the cluster, print only the localhost link.

9. **confusing: `query` cannot show file paths and its help explains nothing.**
   `seqledger query --help` lists `{uniq-id,search,unbacked,mismatches,summary,taxa} [term]` with no description of any of them (cli.py:780-783). `query search Urophycis` returns project, sample, taxon, uniq_id, but no R1/R2 paths, so the CLI cannot answer "where is my FASTQ". `query search Gadidae` returns `(none)` even though the GUI search on the same word finds the cod (GUI searches lineage; CLI does not). `query uniq-id USNM477715` (no space) returns `(none)` with no hint. Missing subcommand errors use internal names: `the following arguments are required: cmd` / `what` / `action`.
   Would have helped: one-line help per lookup, a `query files <term>` (or paths in `search`), and the same search fields as the GUI.

10. **confusing: README is mostly not for me and uses undefined cluster words.**
    The intro (README.md:3-8) leads with "SQLite-backed catalog", "schema validation", "rclone checksum". The user section uses "Store" (README.md:29), "I/O queue" (:59), "qdel" (:73) without explanation. After the user section, the README continues for ~400 lines; only the line at README.md:84 says "The sections below are for data managers", easy to miss. Requirements (README.md:92-93) says "standard library only ... no install step", which contradicts the user section telling me to install conda + pip.
    Would have helped: put user docs in their own short page (or a clear "STOP: users can skip everything below" divider), and a 6-word glossary (Store, Scratch, P-drive, lTIO, qsub/qdel, tunnel).

11. **confusing: `python -m seqledger` only works from the repo folder.**
    From my scratch dir: `No module named seqledger`. README.md:170 says it "Runs without installing too ... from the repo root", but a novice will `cd` to their data and try it there.
    Would have helped: tell users to always install (they do in the user section), and keep the no-install note in the data-manager part.

12. **confusing: Grab & Go wording does not match the buttons.**
    Caption: `Select rows and click **Add to custom table**.` (streamlit_app.py:1123). The buttons are `Add selected (N)` and `Add all matches (N)` (:1133-1140). Also there is no way to add a sample from the Samples view; you must re-search in Grab & Go. Selecting rows means ticking checkboxes at the left edge of the table, which is not mentioned.
    Would have helped: matching button text, and "tick the boxes on the left of each row" in the caption.

13. **confusing: integrity failure does not say which file is bad.**
    I truncated one FASTQ on purpose. Output: `[FAIL] projA: 4 file(s), 3 ok, 1 gzip-error, 0 format-error, 0 unchecked`, exit code 0. No filename. I had to open the GUI Projects view to find it.
    Would have helped: print the bad file path(s) under the FAIL line.

14. **confusing (data-manager path): re-running `taxonomy resolve` wipes the review CSV.**
    After `ingest` wrote taxonomy_review.csv with 4 rows, running `taxonomy resolve` again printed `NCBI: resolved 0 taxa` and `review + edit confirmed_taxid in: .../taxonomy_review.csv`; that file was now header-only. Any edits I had made but not yet applied would be gone. Then `taxonomy apply --review taxonomy_review.csv` said `applied 0 confirmed taxid(s)` with no explanation. Also `taxonomy apply --source worms --review taxonomy_review.csv` (wrong file type) silently applied 0 instead of saying "this is not a WoRMS review file".
    Would have helped: do not overwrite when there is nothing new, or write a dated file; warn on wrong-type review CSV.

15. **confusing: taxonomy download is much bigger than announced.**
    Message: `Downloading NCBI taxdump (~72 MB) to <db dir>/.taxonomy ...` (taxonomy.py:118). Actual disk use after ingest: about 1.4 GB (names.dmp 317 MB, nodes.dmp 232 MB, taxdump.sqlite 784 MB, tar 80 MB) next to the catalog. No progress shown during the ~30 s download. On a home-directory quota this could fail or fill the disk.
    Would have helped: state the real disk need, delete the .dmp/.tar after indexing, show progress.

16. **nit: GUI Taxonomy view shows a raw Python traceback if plotly is missing.**
    In this env plotly was not installed; clicking Taxonomy gave `ModuleNotFoundError: No module named 'plotly'` (streamlit_app.py:844). The conda env and `[gui]` extra do include plotly, so this only bites partial installs, but the message should say how to fix it.

17. **nit: "checksum incomplete (4 uncompared)" on every project, every run.**
    After ingest/validate every project says `checksum incomplete`. For a user this reads like something is broken. It just means the backup comparison has not been run.
    Would have helped: wording like "backup not yet verified".

18. **nit: ingest "next" hint ignores the configured root and contradicts itself.**
    Even with seqdata_root set in config, ingest ends with `seqledger --db scratch.db integrity --seqdata-root <raw_sequence_data>` (cli.py:321-324), a placeholder I do not need. With 0 projects found it prints the WARNING and then `ingest complete. next: run the integrity check`. Errors like the bad-root one also print `== ingest ==` after the error text.

19. **nit: README points to a file that does not exist.**
    README.md:238: `same format as scripts/.../example_map_file.txt`. There is no scripts/ directory and the path has `...` in it. There is also no example `<project>_mapfile.csv` anywhere in the README; I had to read tests/helpers.py to learn the format.

## (c) What worked well

- `--help` exists on every command and most flag descriptions are plain enough.
- Mapfile errors are excellent: the header typo gave `the first columns must be ID,R1,R2,Taxon,UniqID (or UniqueID) ... Got: ID,R1,R2,Taxn,UniqID.` and told me files were still cataloged. Missing/orphan files, empty Taxon, and duplicate IDs were each reported by row number.
- Excel-style mapfile (BOM, CRLF, spaces after commas) ingested fine.
- Running `ingest` twice is safe and says `0 new sample(s), 0 changed`.
- Setting roots once with `init-db` and then just `ingest` with no flags is nice; it echoes which roots it used.
- Bad directory messages are good: `--seqdata-root '/home/.../nope' does not exist or is not a directory -- check the path and that the filesystem (Store/NAS) is mounted.`
- `qsub` missing: `qsub not found -- run gui --qsub on a Hydra login node, or drop --qsub to run the GUI on the current node.` Clear and actionable.
- GUI Grab & Go: search by higher taxon (Gadidae) works, cart shows size, R1/R2 read counts, full paths, NCBI and WoRMS links; CSV, MitoPilot file, and copy script all generated without errors. The copy script checks the destination is writable before starting.
- WoRMS lookup was fast and worked over the network; fuzzy NCBI match caught my deliberate typo (Callinectes sapidis -> sapidus).

## (d) Top 5 recommendations (ranked by impact on a novice)

1. **Add a "Getting your FASTQs" walkthrough in the user section** (README.md:76-83): search -> add -> destination example -> download .job -> `qsub` -> check log; plus "or use the CSV's r1_path/r2_path with cp for a handful of files". Fix the `OWNER` clone URL in the same pass.
2. **Fail fast and honestly in `gui`**: check that the catalog file exists and that streamlit (and plotly) import before printing the tunnel or submitting the job; exit non-zero with "run pip install -e '.[gui]'" or "check the catalog path". On a non-cluster machine, print only the localhost link (gui.py:47-74, gui.py:139-).
3. **Never create a catalog by accident**: use the existing "no catalog at" check (cli.py:41) in integrity, taxonomy, checksums, gui, ingest, and make `init-db --show` read-only. Reword the message for users ("check the path or ask your data manager; data managers: init-db ...") and mention `--db` / `SEQLEDGER_DB`.
4. **Split the README audiences**: a short user page (install, launch, find, grab, troubleshoot, 6-term glossary) and a separate data-manager guide. Remove the "no install step" line from the users' line of sight, and include a 3-line example mapfile.
5. **Make the CLI answer "where is my FASTQ"**: give `query` per-lookup help, include R1/R2 paths (or add `query files`), search lineage like the GUI does, accept `--db` after the subcommand, and name the failing file in `integrity` output.

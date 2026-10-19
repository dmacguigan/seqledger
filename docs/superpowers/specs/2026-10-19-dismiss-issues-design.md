# Dismissable issues in the GUI

Date: 2026-10-19. Status: reviewed, awaiting approval.

## Intent

The GUI lists every problem the checks found: mapfile status per project,
flags per sample, and per-file problems (missing from disk, not in mapfile,
corrupt, empty, md5 mismatch). Many of these are known and accepted (a control
sample with no UniqID, an orphan file kept on purpose, a library that was
re-sequenced). Today they sit in the tables forever, and real new problems hide
among them. Users want to dismiss an issue they have looked at, so the tables
show only what still needs attention, while keeping a visible trace that
something was dismissed and a way to see it again.

Nothing in the catalog changes. Dismissals are display state shared by all GUI
users, stored outside the catalog.

## Goals

- Any GUI user can dismiss a project-level, sample-level, or file-level issue,
  with an optional one-line note.
- A dismissal is shared: every user, on every GUI session, sees it hidden.
- A dismissed issue is never deleted from the catalog. The cell that held it
  shows an OK label with an asterisk so a reader knows something was dismissed.
- A "Show dismissed" toggle reveals dismissed issues with who, when, and the
  note, and lets any user undo a dismissal.
- Status cells are tinted by severity with a colorblind-safe palette so
  problems stand out at a glance.

## Non-goals

- No concatenation, repair, or any action on the data. Dismiss only hides.
- No per-user or private dismissals.
- No dismissing of "not yet done" states: integrity `unchecked`, backup
  `Not verified`, md5 `Not compared`. These clear themselves when the check runs.
- No CLI command for dismissals. The data manager can read or edit the log
  file directly.
- No changes to the CLI checks, their output, or `data_check_issues`.

## Anti-goals

- A dismissal must never silently cover a new or worse problem. If the state
  of an item changes after it was dismissed, the issue shows again.
- The catalog must not be opened for writing by the GUI. Researchers have
  read-only access to the catalog file, each user runs their own GUI process,
  and the admin guide forbids several writers on one SQLite file on the
  network filesystem.
- Color must never be the only signal. Every tinted state also carries a text
  label or an asterisk.

## Constraints

- GUI: Streamlit 1.59, pandas 3, in `seqledger/app/streamlit_app.py`. Core
  modules are stdlib-only; the new storage module must be too.
- The GUI opens the catalog with `db.connect_ro`. Keep that.
- Tables are `st.dataframe` with `on_select="rerun"`. Styler input is supported
  by `st.dataframe` together with `column_config` and selection.
- Data loaders are `@st.cache_data(ttl=60)` keyed by the catalog mtime.
- Tests run with `python -m pytest -q` in the `seqledger` conda env. Existing
  GUI tests live in `tests/test_gui_app.py`.

## Design

### Storage: a hidden append-only log next to the catalog

Path: same directory as the catalog, hidden, named after the catalog file.
For `/store/x/oceandna_catalog.db` it is
`/store/x/.oceandna_catalog_dismissed.tsv`.

The path is stored in the catalog config key `dismiss_log`. `init-db` sets it
to the absolute default path next to the catalog it is run on when the key is
absent (it never overwrites a set value; `init-db --set dismiss_log=/path`
changes it). Config travels inside the `.db`, so a Scratch copy made from the
master points at the master log on Store and never creates its own log on
Scratch. When the key is absent (a catalog not yet re-initialised), the GUI
falls back to the path next to the catalog it opened. When the log is not
readable from where the GUI runs, the GUI shows no dismissals and says why
(see Failure modes).

Format: tab-separated, UTF-8, one event per line, newest last. First line is
a header. Columns:

| column | content |
|---|---|
| `time` | ISO 8601 UTC, seconds, e.g. `2026-10-19T15:04:05Z` |
| `user` | `getpass.getuser()` of the GUI process |
| `action` | `dismiss` or `undo` |
| `scope` | `project`, `sample`, or `file` |
| `project_id` | the project |
| `item` | empty for `project`; `sample_id` for `sample`; `rel_path` for `file` |
| `kind` | the exact state dismissed, see Keys |
| `note` | free text, tabs and newlines replaced by spaces, may be empty |

Append semantics: one `os.open(path, O_WRONLY|O_CREAT|O_APPEND, 0o664)` plus
one `os.write` of the full line. A single short append is the safest
concurrent write on a network filesystem. No locking. After creating the file
the module calls `os.chmod(path, 0o664)` so the umask cannot make it
unwritable for the group.

Read semantics: parse all lines; skip the header, blank lines, and any line
with the wrong column count (log a warning, never raise). The last line per
key wins. The result is `{key: {"action", "time", "user", "note"}}`; keys whose
last action is `undo` count as not dismissed.

Module: `seqledger/dismiss.py`, stdlib only, with roughly:

```
def log_path(db_path, configured=None) -> str
def read(path) -> dict[key, record]          # key = (scope, project_id, item, kind)
def append(path, action, key, user, note="") -> None
def key_for_mapfile(project_id, metadata_status) -> key
def key_for_flags(project_id, sample_id, flags) -> key
def key_for_file(project_id, rel_path, kind) -> key
```

### Keys: a dismissal covers one exact state

The `kind` column encodes the state so that a change in state resurfaces the
issue:

- project scope: `kind = "mapfile:" + metadata_status + ":" + metadata_detail`
  with tabs and newlines in the detail replaced by spaces, e.g.
  `mapfile:flagged:3 of 40 rows flagged ...`. The detail sentence carries the
  counts, so a project that gets worse (more flagged or skipped rows, or a
  broken mapfile) is a new key and shows again. A rewording of the detail on a
  seqledger upgrade also resurfaces it; that is acceptable and rare.
  Dismissing a project's mapfile status and dismissing individual samples'
  flags are independent: dismissing every flagged sample does not change the
  project cell, which has its own Dismiss button.
- sample scope: `kind = "flags:" + flags` where `flags` is the stored
  semicolon-joined string, e.g. `flags:na_uniqid;missing_r2`. A new flag makes
  a new key.
- file scope: `kind` is the issue kind as the GUI already labels it:
  `missing from disk`, `missing from mapfile`, `gzip error`, `format error`,
  `empty (0 reads)`, plus `md5 mismatch` (new, see Rollup). `item` is the
  file's `rel_path`, the physical identity. Note: `load_integrity_issues`
  currently selects `f.filename`; it must select `f.rel_path AS filename` so
  file keys are consistent across the two issue sources.

A dismissal persists until undone. If an issue disappears from the catalog and
later comes back in the same state, it stays dismissed (the toggle shows it).

### Roll-up: project cells count live issues only

`projects_view` builds its status columns from stored counts today. With
dismissals it computes them from the per-file rows minus the dismissed ones:

- `data_files`: live `missing from disk` and `missing from mapfile` rows for
  the project. If the stored `data_check_status` is `issues` and no live rows
  remain, the cell reads `OK *`. `unchecked` is unchanged.
- `integrity`: live corrupt and empty rows. If none remain but some were
  dismissed, the cell reads `OK *` (or `incomplete (n unchecked) *` when
  unchecked files remain; unchecked count comes from the stored totals as now).
- `checksum`: live `md5 mismatch` rows. To make this possible,
  `load_data_issues` or a sibling loader adds one row per file with
  `md5_match = 0`, kind `md5 mismatch`, so the project issue table lists md5
  mismatches alongside the other file problems. `n_uncompared` stays stored.
- `mapfile`: `OK *` when the key for the current status and detail is
  dismissed.

Partly dismissed cells: when some but not all of a cell's issues are
dismissed, the cell shows the live count with a trailing asterisk, e.g.
`3 file(s) on disk, not in mapfile *`, and takes the tint of the live
severity (error or warning), not the dismissed tint. `OK *` with the dismissed
tint is only for a cell whose issues are all dismissed. With Show dismissed
on, a cell shows the full count and how many are dismissed, e.g.
`5 file(s) on disk, not in mapfile (2 dismissed)`. The checksum cell follows
the integrity pattern: `OK *` when every mismatch is dismissed and nothing is
uncompared, `incomplete (n not compared) *` otherwise.

The sidebar "Only ... issues" checkboxes filter on live issues.

The dismissed label is the same everywhere: `OK *`. It does not claim the
underlying check passed (a dismissed md5 mismatch is not "Verified"); the
caption under each table says `* an issue here was dismissed; turn on
"Show dismissed" to see it`.

### Views and buttons

A sidebar checkbox **Show dismissed** (session-wide, `st.session_state`)
appears on the Projects, Samples, and Files views.

**Projects view**

- Main table: no buttons. Cells roll up as above. With Show dismissed on,
  a rolled-up cell shows its original label plus ` (dismissed)` instead of
  `OK *`.
- Selected project, mapfile banner: shown only when the status is not `ok`
  and not dismissed. Next to it a **Dismiss** button and a note input.
  Dismissed, the banner is replaced by a one-line info `Mapfile: OK *` with
  user, date, note, and an **Undo** button when Show dismissed is on.
- Selected project, "Data-files issues" table: lists live file issues
  (data-check, integrity, md5 mismatch). `selection_mode="multi-row"`. Under
  it: a note input, **Dismiss selected (n)**, and **Dismiss all (N)**. With
  Show dismissed on, dismissed rows are included with three extra columns
  `dismissed_by`, `dismissed_on`, `note`, and the buttons become
  **Undo selected** for dismissed rows (a mixed selection applies dismiss to
  live rows and undo to dismissed rows; keep it simple: two buttons, each acts
  on the rows it applies to).

**Samples view**

- Main table: `flags` cell shows `OK *` when that sample's current flags key is
  dismissed (empty when the sample never had flags). When the selected sample
  has live flags, a **Dismiss flags for this sample** button and note input
  appear under the table. With Show dismissed on and the flags dismissed, the
  cell shows the flags plus ` (dismissed)` and an **Undo** button appears.
- Files list under the sample: read-only; its `integrity` and `backup` cells
  reflect file dismissals.

**Files view**

- `integrity` and `backup` cells show `OK *` when that file's problem is
  dismissed. When the selected file has a live corrupt, empty, or md5 mismatch
  state, a **Dismiss** button and note input appear under the table, one per
  live problem (a file can have both an integrity and a checksum problem).
  Keys are identical to the Projects issue table so the two stay in sync.
- The sidebar `Integrity` and `Backup status` filters match on the live state,
  so a dismissed corrupt file is not returned by `gzip_error`.

**Grab & Go**

- No buttons. `_file_problem` treats a dismissed problem as `ok`, so a file
  whose every problem is dismissed is included in the copy job, the size
  estimate, and the MitoPilot map like any healthy file, and the "Exclude
  problem files" box no longer drops it. The per-sample `files_ok` summary
  reads `ok *` when any of the sample's files had a dismissed problem. A
  dismissed "missing from disk" file will still fail inside rclone; the job's
  own error output reports it. Update
  `test_grab_and_go_flags_problem_files_and_lane_splits` accordingly.

**Taxonomy**: untouched.

Button mechanics: on click, `dismiss.append(...)` for each key, then
`st.rerun()`. The dismissals loader is `@st.cache_data` keyed by the log's
mtime and size so a rerun sees the new line; when the log is missing, the
loader returns an empty dict.

### Colors

Okabe-Ito palette, applied as translucent backgrounds so they work on both
Streamlit themes. Only status cells are tinted, never whole rows.

| state | fill | examples |
|---|---|---|
| error | `rgba(213, 94, 0, 0.30)` (vermillion) | corrupt, empty, missing from disk, md5 mismatch, no mapfile, broken mapfile, no usable rows, no data folder |
| warning | `rgba(230, 159, 0, 0.35)` (orange) | missing from mapfile, flagged rows, sample flags |
| dismissed | `rgba(86, 180, 233, 0.35)` (sky blue) | any `OK *` or `(dismissed)` cell |
| pending | `rgba(187, 187, 187, 0.35)` (grey) | unchecked, Not verified, Not compared, incomplete |
| ok | none | everything else |

Tinted columns: Projects `mapfile`, `data_files`, `checksum`, `integrity`;
Samples `flags`, `backup`; Files `backup`, `integrity`; Grab & Go `files_ok`;
the project issue table's `issue` column (error or warning by kind, dismissed
when listed under Show dismissed); the per-sample files list `backup` and
`integrity`.

Implementation: one helper `_tint(df, columns)` returning
`df.style.map(css_for_label, subset=columns)` where `css_for_label` maps the
cell text to a `background-color`. The label-to-severity mapping lives in one
dict next to `_ISSUE_LABEL`. The Styler is passed to `st.dataframe` in place
of the frame; `column_config` and selection keep working. The CSV download
keeps using the plain frame.

### Failure modes

- Log not readable (missing is fine, treated as empty; a permission error or a
  Scratch copy on a node without Store): the tables show no dismissals, and a
  sidebar caption says `Dismissals unavailable: cannot read <path>`. Buttons
  are hidden.
- Log readable but not writable: buttons stay visible; a failed append shows
  `st.error("Could not record the dismissal: <path> is not writable. Ask your
  data manager to run: chmod 664 <path>")` and nothing else changes.
- Malformed line in the log: skipped, counted, shown once in the sidebar
  caption as `n unreadable line(s) in <path>`.
- Two users dismiss at the same instant: two appended lines, both applied.
  The order only matters for the same key, where either outcome is correct.

### Documentation and changelog

- `docs/user-guide.md`: a "Dismissing issues" section: what dismiss does and
  does not do, the toggle, the asterisk, the colors and their meaning.
- `docs/admin-guide.md`: in Permissions, the log file, its name, that the
  first dismiss creates it with mode 664, and the one-time `touch` + `chmod 664`
  (or 666 with no shared group) to pre-create it; the `dismiss_log` config
  key for Scratch copies; a note that the log is plain text and safe to edit
  or truncate.
- `docs/reference/config.md`: the `dismiss_log` key.
- `CHANGELOG.md` under Unreleased, Added.
- No schema change.

## Decisions already made (by the user)

- Any GUI user can dismiss; dismissals are shared across users.
- Storage is a hidden sidecar file next to the catalog, not a catalog table.
- Scopes: project, sample, and file.
- Dismissed cells show the OK message with a red asterisk; a toggle shows or
  hides dismissed items.
- "Not yet done" states cannot be dismissed.
- A file whose problems are all dismissed goes into the Grab & Go copy job.
- Buttons live where listed under Views and buttons.
- Colorblind-safe palette for status cells.

## Calls made in this design (check these)

- Append-only TSV with last-line-wins, rather than a rewritten file.
- Keys encode the exact state so a changed state shows again.
- Dismissals persist until undone, even if the issue goes away in between.
- The project mapfile key includes the detail sentence, so a change in counts
  resurfaces it. Sample-flag dismissals and the project mapfile dismissal are
  independent.
- `init-db` records the default log path in config so Scratch copies inherit
  the master's log.
- Partly dismissed cells keep the live label and severity tint, with an
  asterisk.
- The dismissed label is `OK *` everywhere, including for backup and
  integrity cells, rather than a per-column "verified" wording.
- md5 mismatches are added to the project issue table so every file-level
  problem is listed and dismissable in one place.
- `dismiss_log` config key for Scratch copies; without Store mounted, the
  feature degrades to read-only or unavailable rather than writing to Scratch.
- Username is the account running the GUI process. If one person serves the
  GUI for others, every dismissal carries that person's name.
- Tints are translucent Okabe-Ito colors; OK cells get no fill.

## Left to the builder

- Exact widget layout under each table (columns, spacing, button labels).
- Whether the Styler is built once per view or per table; keep it to one
  helper.
- How to render the red asterisk inside a dataframe cell. If Streamlit cannot
  color part of a cell, the whole cell takes the dismissed tint and the
  asterisk stays plain text.
- Test shape: unit tests for `dismiss.py` (append, read, last-wins, bad line,
  create with 664, unwritable path) and for the roll-up label helpers given
  frames plus a dismissed dict; GUI rendering is not unit-tested beyond what
  `tests/test_gui_app.py` already covers.

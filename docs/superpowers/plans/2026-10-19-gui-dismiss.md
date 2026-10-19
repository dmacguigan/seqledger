# Dismissable GUI issues Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let any GUI user dismiss project, sample, and file issues, shared through a hidden append-only log next to the catalog, with "OK *" labels, a Show-dismissed toggle, and colorblind-safe cell tints.

**Architecture:** A new stdlib module `seqledger/dismiss.py` owns the log file (path, append, read, keys). `init-db` records the log path in catalog config. The Streamlit app reads the log through a cached loader, derives live-vs-dismissed state with pure helper functions (unit-tested without Streamlit), and renders buttons and tinted cells in the Projects, Samples, Files, and Grab & Go views.

**Tech Stack:** Python 3.10+, stdlib for `dismiss.py`; Streamlit 1.59, pandas 3 for the app; pytest with `streamlit.testing.v1.AppTest` for view tests.

**Spec:** `docs/superpowers/specs/2026-10-19-dismiss-issues-design.md`

## Global Constraints

- `seqledger/dismiss.py` is stdlib-only. No new dependencies anywhere.
- The GUI keeps opening the catalog with `odb.connect_ro`. It never writes to the catalog.
- Log format: TSV, UTF-8, header line `time\tuser\taction\tscope\tproject_id\titem\tkind\tnote`, one event per line, appended with a single `os.write`; file created with mode `0o664` then `os.chmod(path, 0o664)`.
- Key tuple everywhere: `(scope, project_id, item, kind)` with `scope` in `project|sample|file`.
- Kinds: project `"mapfile:" + metadata_status + ":" + detail_one_line`; sample `"flags:" + flags`; file one of `missing from disk`, `missing from mapfile`, `gzip error`, `format error`, `empty (0 reads)`, `md5 mismatch`.
- Dismissed label everywhere: `OK *`. Partly dismissed: live label + ` *`. With Show dismissed on: full label + ` (n dismissed)` or label + ` (dismissed)`.
- Tints (background-color): error `rgba(213, 94, 0, 0.30)`, warning `rgba(230, 159, 0, 0.35)`, dismissed `rgba(86, 180, 233, 0.35)`, pending `rgba(187, 187, 187, 0.35)`, ok none.
- Not dismissable: integrity `unchecked`, backup `Not verified`, md5 `Not compared`.
- No non-ASCII characters in code. Minimal comments. No em dashes in docs.
- Commit after each task; `python -m pytest -q` green before each commit.

## Review Focus

1. Log file with Windows line endings or a trailing partial line (crash mid-append): `read` must skip the partial line and count it, never raise. Test in Task 1.
2. A note containing a tab or newline: must be flattened to spaces so the line stays one record. Test in Task 1.
3. Project detail text containing a tab (metadata_detail is free text from ingest): the project key must flatten it the same way. Test in Task 3.
4. Dismiss button clicked while the log is unwritable (group permission missing): the app must show the chmod hint and keep rendering, not crash. Test in Task 4.
5. The same rel_path listed under two issue kinds (missing from disk and md5 mismatch): dismissing one must not hide the other. Test in Task 3.

---

### Task 1: `seqledger/dismiss.py` log module

**Files:**
- Create: `seqledger/dismiss.py`
- Test: `tests/test_dismiss.py`

**Interfaces:**
- Produces:
  - `HEADER = "time\tuser\taction\tscope\tproject_id\titem\tkind\tnote"`
  - `def default_log_path(db_path: str) -> str`: `os.path.join(dirname, "." + stem + "_dismissed.tsv")` where stem is the catalog basename without its extension; returns an absolute path.
  - `def flatten(text) -> str`: `None` -> `""`; tabs, `\r`, `\n` replaced by single spaces; stripped.
  - `def project_key(project_id, metadata_status, metadata_detail) -> tuple`: `("project", project_id, "", "mapfile:" + status + ":" + flatten(detail))`.
  - `def sample_key(project_id, sample_id, flags) -> tuple`: `("sample", project_id, sample_id, "flags:" + flatten(flags))`.
  - `def file_key(project_id, rel_path, kind) -> tuple`: `("file", project_id, rel_path, kind)`.
  - `def append(path, action, key, user, note="") -> None`: `action` in `("dismiss", "undo")` else `ValueError`; writes `time\tuser\taction\tscope\tproject\titem\tkind\tnote\n` with `time = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")`; creates the file with the header line on first write (`os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o664)`, then `os.chmod(path, 0o664)` when it was just created). Propagates `OSError` (the GUI handles it).
  - `def read(path) -> tuple[dict, int]`: `(dismissed, n_bad)`. `dismissed` maps key -> `{"time", "user", "note"}` for keys whose last action is `dismiss`. Missing file -> `({}, 0)`. Lines are split on `\t` after stripping `\r\n`; header, blank, and lines with a column count other than 8 are skipped, the malformed ones counted in `n_bad`.

- [ ] **Step 1: Write the failing tests**

```python
import os, stat
import pytest
from seqledger import dismiss

def test_default_log_path_is_hidden_sibling(tmp_path):
    db = str(tmp_path / "oceandna_catalog.db")
    assert dismiss.default_log_path(db) == str(tmp_path / ".oceandna_catalog_dismissed.tsv")

def test_keys():
    assert dismiss.project_key("P1", "flagged", "3 of 40\trows\nflagged") == \
        ("project", "P1", "", "mapfile:flagged:3 of 40 rows flagged")
    assert dismiss.sample_key("P1", "s1", "na_uniqid;missing_r2") == \
        ("sample", "P1", "s1", "flags:na_uniqid;missing_r2")
    assert dismiss.file_key("P1", "P1/Run1/x.fq.gz", "md5 mismatch") == \
        ("file", "P1", "P1/Run1/x.fq.gz", "md5 mismatch")

def test_append_creates_664_with_header_and_read_round_trips(tmp_path):
    p = str(tmp_path / ".cat_dismissed.tsv")
    k = dismiss.file_key("P1", "P1/x.fq.gz", "gzip error")
    dismiss.append(p, "dismiss", k, "dan", "known\tbad\nfile")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o664
    lines = open(p).read().splitlines()
    assert lines[0] == dismiss.HEADER
    assert lines[1].split("\t")[1:] == ["dan", "dismiss", "file", "P1", "P1/x.fq.gz",
                                        "gzip error", "known bad file"]
    d, n_bad = dismiss.read(p)
    assert n_bad == 0 and set(d) == {k}
    assert d[k]["user"] == "dan" and d[k]["note"] == "known bad file"
    assert d[k]["time"].endswith("Z")

def test_last_action_wins_and_undo_removes(tmp_path):
    p = str(tmp_path / "log.tsv")
    k = dismiss.sample_key("P1", "s1", "na_uniqid")
    dismiss.append(p, "dismiss", k, "a")
    dismiss.append(p, "undo", k, "b")
    assert dismiss.read(p)[0] == {}
    dismiss.append(p, "dismiss", k, "c", "again")
    assert dismiss.read(p)[0][k]["user"] == "c"

def test_read_missing_file_and_bad_lines(tmp_path):
    assert dismiss.read(str(tmp_path / "none.tsv")) == ({}, 0)
    p = tmp_path / "log.tsv"
    p.write_text(dismiss.HEADER + "\r\n"
                 "2026-01-01T00:00:00Z\tu\tdismiss\tfile\tP1\tP1/a.gz\tgzip error\t\r\n"
                 "\n"
                 "garbage line\n"
                 "2026-01-01T00:00:01Z\tu\tdismiss\tfile\tP1\tP1/b.gz")  # partial line
    d, n_bad = dismiss.read(str(p))
    assert set(d) == {("file", "P1", "P1/a.gz", "gzip error")}
    assert n_bad == 2

def test_append_rejects_bad_action_and_propagates_oserror(tmp_path):
    with pytest.raises(ValueError):
        dismiss.append(str(tmp_path / "l.tsv"), "hide", dismiss.file_key("P", "f", "gzip error"), "u")
    with pytest.raises(OSError):
        dismiss.append(str(tmp_path / "missing_dir" / "l.tsv"),
                       "dismiss", dismiss.file_key("P", "f", "gzip error"), "u")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest -q tests/test_dismiss.py`
Expected: FAIL, `ModuleNotFoundError: seqledger.dismiss`

- [ ] **Step 3: Implement `seqledger/dismiss.py`** with the interface above. Module docstring: two lines on what the log is and that last line per key wins.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest -q tests/test_dismiss.py`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add seqledger/dismiss.py tests/test_dismiss.py
git commit -m "dismiss: append-only log of dismissed GUI issues"
```

---

### Task 2: `dismiss_log` config key recorded by `init-db`

**Files:**
- Modify: `seqledger/db.py:26-38` (CONFIG_DEFAULTS)
- Modify: `seqledger/cli.py:75-112` (`cmd_init_db`)
- Modify: `docs/reference/config.md` (Keys table)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `dismiss.default_log_path(db_path)` from Task 1.
- Produces: config key `dismiss_log` (default `""`). After `init-db` on a catalog where the key is unset or empty, `get_config(conn, "dismiss_log")` is the absolute default path next to that catalog. A set value is never overwritten; `--set dismiss_log=/x` still works.

- [ ] **Step 1: Write the failing test** in `tests/test_cli.py`, using the file's existing `_run` helper:

```python
def test_init_db_records_dismiss_log_once(tmp_path):
    from seqledger import db as odb
    db = str(tmp_path / "cat.db")
    assert _run(["--db", db, "init-db"]) == 0
    con = odb.connect(db)
    assert odb.get_config(con, "dismiss_log") == str(tmp_path / ".cat_dismissed.tsv")
    con.close()
    assert _run(["--db", db, "init-db", "--set", "dismiss_log=/store/x/.log.tsv"]) == 0
    assert _run(["--db", db, "init-db"]) == 0
    con = odb.connect(db)
    assert odb.get_config(con, "dismiss_log") == "/store/x/.log.tsv"
    con.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest -q tests/test_cli.py -k dismiss_log`
Expected: FAIL, `"" != ".../.cat_dismissed.tsv"` (or unknown config key)

- [ ] **Step 3: Implement.** Add `"dismiss_log": ""` to `CONFIG_DEFAULTS` with the comment `# GUI dismissals log (set by init-db)`. In `cmd_init_db`, after applying `updates`, if `not odb.get_config(conn, "dismiss_log")` then `odb.set_config(conn, "dismiss_log", dismiss.default_log_path(os.path.abspath(args.db)))`. Add the row to the Keys table in `docs/reference/config.md`: default `(set by init-db)`, set with `--set dismiss_log=`, used by `GUI`, notes `Path of the hidden log where GUI users' dismissed issues are recorded. init-db fills it with the path next to the catalog it runs on, so copies of the catalog point back at the master's log. Change it only if the log must live elsewhere.`

- [ ] **Step 4: Run the suite**

Run: `python -m pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add seqledger/db.py seqledger/cli.py docs/reference/config.md tests/test_cli.py
git commit -m "init-db: record dismiss_log path in config"
```

---

### Task 3: Pure GUI helpers: loaders, issue keys, roll-up labels, tints

**Files:**
- Modify: `seqledger/app/streamlit_app.py` (new helpers near `_ISSUE_LABEL`, `load_*`, `integrity_label`, `_file_problem`, `_files_ok`)
- Test: `tests/test_gui_app.py`

**Interfaces:**
- Consumes: `dismiss.read`, `dismiss.default_log_path`, `dismiss.project_key`, `dismiss.sample_key`, `dismiss.file_key`.
- Produces (all in `streamlit_app.py`):
  - `def _dismiss_log_path(db_path) -> str`: `_config(db_path, "dismiss_log") or dismiss.default_log_path(db_path)`.
  - `@st.cache_data(ttl=60) def load_dismissals(path, mtime, size) -> tuple[dict, int]`: wraps `dismiss.read`; callers pass `os.path.getmtime`/`getsize` when the file exists else `0, 0`.
  - `def load_md5_issues(db_path, mtime) -> DataFrame`: same columns as `load_data_issues` (`project_id, kind, filename, full_path, sample_id, direction, taxon, uniq_id, uniq_id_url`), `kind = 'md5 mismatch'`, `filename = f.rel_path`, rows where `f.md5_match = 0`. Cached like the others.
  - `load_integrity_issues` changes `f.filename` to `f.rel_path AS filename`.
  - `load_files` adds `f.rel_path`.
  - `_ISSUE_LABEL` and `_ISSUE_DETAIL` gain `"md5 mismatch"`: label `md5 mismatch`, detail `Store and backup md5 differ for this file (one copy changed or is corrupt).`
  - `def _issue_key(row) -> tuple`: `dismiss.file_key(row["project_id"], row["filename"], row["kind"])`.
  - `def _mark_issues(issues: DataFrame, dismissed: dict) -> DataFrame`: copy with columns `dismissed` (bool), `dismissed_by`, `dismissed_on`, `note` (empty strings when live).
  - `def _project_status(prow, issues, dismissed, show_dismissed) -> dict`: keys `mapfile, data_files, checksum, integrity`, each a display string; `prow` is one `load_projects` row (uses `metadata_status, metadata_detail, data_check_status, data_check_n_missing, data_check_n_orphan, n_files, n_integrity_ok, n_integrity_bad, n_integrity_empty, n_mismatch, n_uncompared`); `issues` is the project's combined issue rows (data + integrity + md5) already passed through `_mark_issues`. Rules:
    - mapfile: `_MAPFILE_LABEL[status]`; if `dismiss.project_key(...)` in `dismissed`: `OK *`, or label + ` (dismissed)` when `show_dismissed`.
    - data_files: counts live rows of kinds `missing from disk` / `missing from mapfile`; `ovalidate.data_label` on live counts; if any such row is dismissed: all dismissed -> `OK *` (show: full label + ` (n dismissed)`), some -> live label + ` *` (show: full label + ` (n dismissed)`). `unchecked` status unchanged.
    - integrity: same pattern over kinds `gzip error`, `format error`, `empty (0 reads)`. Live problems are the live row counts per kind. Unchecked remainder is `rest = n_files - n_integrity_ok - n_integrity_bad` using the STORED counts (a dismissed corrupt file was still checked). Labels: live problems > 0 -> `integrity_label` text built from live counts, plus ` *` if any dismissed; no live problems and some dismissed -> `OK *` when `rest == 0`, else `incomplete (rest unchecked) *`; show_dismissed -> `integrity_label` on stored counts + ` (n dismissed)`.
    - checksum: same pattern over `md5 mismatch` via `ovalidate.checksum_label` with live `n_mismatch`; all dismissed -> `OK *` when `n_uncompared == 0` else `incomplete (n not compared) *`.
  - `def _severity(label) -> str` returning `error|warning|pending|dismissed|ok`. Non-string or empty -> `ok`. Order of checks: (1) `label == "OK *"` or `label.endswith("(dismissed)")` -> `dismissed`; (2) strip a trailing ` *` and a trailing ` (n dismissed)` suffix, then classify the rest: error if it is one of `no mapfile`, `no data folder`, `broken mapfile`, `no usable rows`, `Mismatch`, `gzip_error`, `format_error`, `gzip error`, `format error`, `empty (0 reads)`, `missing from disk`, `md5 mismatch`, or contains `corrupt`, `empty`, `in mapfile not on disk`, or `differ between Store`; warning if `flagged rows`, `missing from mapfile`, contains `not in mapfile`, or every `;`-separated token is a key of `ovalidate.FLAG_MESSAGES`; pending if `unchecked`, `Not verified`, `Not compared`, `no files`, or starts with `incomplete`, `backup not yet verified`, or `unchecked (`; else `ok`.
  - `_TINT = {"error": "background-color: rgba(213, 94, 0, 0.30)", ...}` per Global Constraints; `ok` -> `""`.
  - `def _tint(df, columns) -> Styler`: `df.style.map(lambda v: _TINT[_severity(v)], subset=[c for c in columns if c in df.columns])`.
  - `_file_problem(r, dismissed=frozenset())`: when the problem's key `dismiss.file_key(r["project_id"], r["rel_path"], kind)` is in `dismissed`, returns `"dismissed"`; kind mapping: `missing` -> `missing from disk`, `corrupt` -> `gzip error` or `format error` by `integrity_status`, `empty` -> `empty (0 reads)`.
  - `_files_ok(problems)`: `"dismissed"` counts as ok; result gets ` *` appended when any problem was `dismissed` (`ok *`, or `1 corrupt *`).

- [ ] **Step 1: Write the failing tests** in `tests/test_gui_app.py`:

```python
def _issues_df(rows):
    cols = ["project_id", "kind", "filename"]
    return pd.DataFrame(rows, columns=cols).assign(full_path="", sample_id="", direction="",
                                                   taxon="", uniq_id="", uniq_id_url="")

def _prow(**kw):
    base = dict(project_id="P1", metadata_status="ok", metadata_detail="", data_check_status="issues",
                data_check_n_missing=0, data_check_n_orphan=0, n_files=4, n_integrity_ok=4,
                n_integrity_bad=0, n_integrity_empty=0, n_mismatch=0, n_uncompared=0)
    base.update(kw); return pd.Series(base)

def test_project_status_rolls_up_live_and_dismissed():
    from seqledger import dismiss
    issues = _issues_df([("P1", "missing from mapfile", "P1/Run1/a.gz"),
                         ("P1", "missing from mapfile", "P1/Run2/a.gz"),
                         ("P1", "missing from disk", "P1/b.gz"),
                         ("P1", "md5 mismatch", "P1/b.gz"),
                         ("P1", "gzip error", "P1/c.gz")])
    prow = _prow(data_check_n_missing=1, data_check_n_orphan=2, n_integrity_bad=1,
                 n_integrity_ok=3, n_mismatch=1)
    none = app._mark_issues(issues, {})
    s = app._project_status(prow, none, {}, False)
    assert s["data_files"] == "1 file(s) in mapfile not on disk, 2 file(s) on disk, not in mapfile"
    assert s["integrity"] == "1 corrupt" and s["checksum"] == "1 file(s) differ between Store and P-drive"
    d = {dismiss.file_key("P1", "P1/Run1/a.gz", "missing from mapfile"): {"user": "u", "time": "t", "note": ""},
         dismiss.file_key("P1", "P1/b.gz", "missing from disk"): {"user": "u", "time": "t", "note": ""},
         dismiss.file_key("P1", "P1/c.gz", "gzip error"): {"user": "u", "time": "t", "note": ""}}
    marked = app._mark_issues(issues, d)
    assert list(marked["dismissed"]) == [True, False, True, False, True]
    s = app._project_status(prow, marked, d, False)
    assert s["data_files"] == "1 file(s) on disk, not in mapfile *"
    assert s["integrity"] == "OK *"
    assert s["checksum"] == "1 file(s) differ between Store and P-drive"   # md5 key untouched
    s = app._project_status(prow, marked, d, True)
    assert s["data_files"] == "1 file(s) in mapfile not on disk, 2 file(s) on disk, not in mapfile (2 dismissed)"
    assert s["integrity"] == "1 corrupt (1 dismissed)"

def test_project_status_mapfile_and_incomplete_variants():
    from seqledger import dismiss
    prow = _prow(metadata_status="flagged", metadata_detail="3 rows\tflagged", n_integrity_ok=2,
                 n_integrity_bad=1, n_uncompared=2, n_mismatch=1)
    issues = app._mark_issues(_issues_df([("P1", "gzip error", "P1/c.gz"),
                                          ("P1", "md5 mismatch", "P1/c.gz")]), {})
    s = app._project_status(prow, issues, {}, False)
    assert s["mapfile"] == "flagged rows"
    d = {dismiss.project_key("P1", "flagged", "3 rows\tflagged"): {"user": "u", "time": "t", "note": ""},
         dismiss.file_key("P1", "P1/c.gz", "gzip error"): {"user": "u", "time": "t", "note": ""},
         dismiss.file_key("P1", "P1/c.gz", "md5 mismatch"): {"user": "u", "time": "t", "note": ""}}
    issues = app._mark_issues(issues, d)
    s = app._project_status(prow, issues, d, False)
    assert s["mapfile"] == "OK *"
    assert s["integrity"] == "incomplete (1 unchecked) *"
    assert s["checksum"] == "incomplete (2 not compared) *"
    assert app._project_status(prow, issues, d, True)["mapfile"] == "flagged rows (dismissed)"

def test_severity_and_tint():
    sv = app._severity
    assert sv("gzip error") == "error" and sv("1 corrupt") == "error" and sv("Mismatch") == "error"
    assert sv("missing from mapfile") == "warning" and sv("na_uniqid") == "warning"
    assert sv("unchecked") == "pending" and sv("incomplete (2 unchecked)") == "pending"
    assert sv("OK *") == "dismissed" and sv("OK") == "ok" and sv("") == "ok" and sv("Verified") == "ok"
    assert sv("1 corrupt *") == "error"
    styler = app._tint(pd.DataFrame({"integrity": ["gzip error", "ok"], "x": [1, 2]}), ["integrity", "nope"])
    html = styler.to_html()
    assert "rgba(213, 94, 0, 0.30)" in html

def test_file_problem_honours_dismissals():
    from seqledger import dismiss
    r = {"project_id": "P1", "rel_path": "P1/x.gz", "integrity_status": "gzip_error", "n_reads": None}
    assert app._file_problem(r) == "corrupt"
    d = {dismiss.file_key("P1", "P1/x.gz", "gzip error"): {}}
    assert app._file_problem(r, d) == "dismissed"
    m = {"project_id": "P1", "rel_path": "P1/y.gz", "integrity_status": "ok", "n_reads": 5, "missing_on_disk": 1}
    assert app._file_problem(m, {dismiss.file_key("P1", "P1/y.gz", "missing from disk"): {}}) == "dismissed"
    assert app._files_ok(["ok", "dismissed"]) == "ok *"
    assert app._files_ok(["corrupt", "dismissed"]) == "1 corrupt *"

def test_md5_issues_loader(tmp_path):
    p = _grab_catalog(tmp_path)
    con = sqlite3.connect(p)
    con.execute("UPDATE files SET md5_match=0 WHERE rel_path='P1/s2_R2.fastq.gz'")
    con.commit(); con.close()
    df = app.load_md5_issues(p, os.path.getmtime(p))
    assert list(df["kind"]) == ["md5 mismatch"] and list(df["filename"]) == ["P1/s2_R2.fastq.gz"]
    integ = app.load_integrity_issues(p, os.path.getmtime(p))
    assert list(integ["filename"]) == ["P1/s2_R1.fastq.gz"]   # rel_path, not basename
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest -q tests/test_gui_app.py -k "project_status or severity or honours or md5_issues"`
Expected: FAIL with AttributeError on the new names

- [ ] **Step 3: Implement** the helpers listed under Produces. `_project_status` builds live counts with `issues[~issues["dismissed"]]["kind"].isin(kinds).sum()` and dismissed counts likewise; keep each column's logic in one small function if that reads better, but keep `_project_status` as the single entry point.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest -q tests/test_gui_app.py`
Expected: all pass (existing `test_file_problem_and_files_ok` still passes since `dismissed` defaults to empty)

- [ ] **Step 5: Commit**

```bash
git add seqledger/app/streamlit_app.py tests/test_gui_app.py
git commit -m "gui: dismissal helpers, roll-up labels, cell tints"
```

---

### Task 4: Projects view: toggle, tinted table, dismiss and undo buttons

**Files:**
- Modify: `seqledger/app/streamlit_app.py` (`projects_view`, `main`)
- Test: `tests/test_gui_app.py`

**Interfaces:**
- Consumes: Task 3 helpers; `dismiss.append`, `dismiss.project_key`.
- Produces:
  - `def _dismiss_state() -> tuple[str, dict, str | None]`: `(log_path, dismissed, problem)` where `problem` is `None`, or `"Dismissals unavailable: cannot read <path>"` on a read `OSError`, or `"<n> unreadable line(s) in <path>"`. Uses `load_dismissals`.
  - `def _dismiss_sidebar() -> tuple[dict, bool]`: renders the sidebar checkbox `Show dismissed` (key `show_dismissed`) and the caption for `problem`; returns `(dismissed, show_dismissed)`. Called by `projects_view`, `samples_view`, `files_view` at the END of their `with st.sidebar:` block (after the existing filters, so it is the last sidebar checkbox), and by `custom_table_view` via `_dismiss_state()` only (no checkbox).
  - `def _record(log_path, action, keys, note) -> bool`: appends each key with `getpass.getuser()`; on `OSError` shows `st.error(f"Could not record the dismissal: {log_path} is not writable. Ask your data manager to run: chmod 664 {log_path}")` and returns False; on success calls `load_dismissals.clear()` and returns True. Callers `st.rerun()` on True.
  - `main()` passes `load_md5_issues(...)` as a fourth frame to `projects_view(df, issues, integ_issues, md5_issues)`.

Behavior in `projects_view`:
- Combined issues = concat of the three frames, `_mark_issues` applied once.
- Main table columns `mapfile, data_files, checksum, integrity` come from `_project_status` per row; "Only ... issues" checkboxes test the live labels (`mapfile != "OK"` and not starting with `OK`; `data_files`/`checksum`/`integrity` severity in `error|warning`).
- Table rendered as `_tint(show[cols], ["mapfile", "data_files", "checksum", "integrity"])`; caption gains `"* an issue here was dismissed; turn on 'Show dismissed' to see it."`
- Selected project: mapfile banner only when status != ok and its key not dismissed; beside it `st.text_input("Note", key="note_mapfile")` and `st.button("Dismiss mapfile issue")`. When dismissed: `st.info(f"Mapfile: OK * (dismissed by {user} on {date}{': ' + note if note})")` plus `st.button("Undo")` when `show_dismissed`.
- Issue table: rows = live rows, plus dismissed rows when `show_dismissed` (extra columns `dismissed_by, dismissed_on, note` shown then). When there are no rows to show: `st.info(f"{n} issue(s) dismissed; turn on 'Show dismissed' to see them.")` if any are dismissed, else the existing "No recorded data-files or integrity issues" info. `selection_mode="multi-row"`, key `f"issues_table_{pid}"`. `issue` column tinted via `_tint`. Under it: `st.text_input("Note", key="note_issues")`, `st.button(f"Dismiss selected ({n_live_sel})", disabled=...)`, `st.button(f"Dismiss all ({n_live})", disabled=...)`, and when `show_dismissed` `st.button(f"Undo selected ({n_dismissed_sel})", disabled=...)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_gui_app.py`:

```python
def _issues_catalog(tmp_path):
    p = _grab_catalog(tmp_path)
    con = sqlite3.connect(p)
    con.execute("UPDATE projects SET data_check_status='issues', data_check_n_missing=1, "
                "metadata_status='flagged', metadata_detail='1 row flagged' WHERE project_id='P1'")
    con.execute("UPDATE files SET md5_match=0 WHERE rel_path='P1/s2_R2.fastq.gz'")
    con.commit(); con.close()
    return p

def _select(at, key, rows):
    at.session_state[key] = {"selection": {"rows": rows, "columns": []}}
    return at.run()

def _button(at, label):
    return next(b for b in at.button if b.label.startswith(label))

def test_projects_dismiss_all_then_show_and_undo(tmp_path, monkeypatch):
    from seqledger import dismiss
    p = _issues_catalog(tmp_path)
    at = _apptest(monkeypatch, p, "Projects")
    _select(at, _table_key(at, "projects_table_"), [0])
    assert at.dataframe[0].value.iloc[0]["data_files"] == "1 file(s) in mapfile not on disk"
    assert any("1 row flagged" in w.value for w in at.warning)
    issues = at.dataframe[1].value
    assert sorted(issues["issue"]) == ["gzip error", "md5 mismatch", "missing from disk"]
    _button(at, "Dismiss all (3)").click().run()
    _select(at, _table_key(at, "projects_table_"), [0])
    row = at.dataframe[0].value.iloc[0]
    assert row["data_files"] == "OK *" and row["integrity"] == "OK *" and row["checksum"] == "OK *"
    assert "Data-files issues" in " ".join(h.value for h in at.subheader)
    assert len(at.dataframe) == 1   # no live rows: table replaced by an info line
    assert any("3 issue(s) dismissed" in i.value for i in at.info)
    log, _ = dismiss.read(dismiss.default_log_path(p))
    assert len(log) == 3
    # mapfile dismiss
    _button(at, "Dismiss mapfile issue").click().run()
    _select(at, _table_key(at, "projects_table_"), [0])
    assert at.dataframe[0].value.iloc[0]["mapfile"] == "OK *"
    assert not at.warning
    assert any("Mapfile: OK *" in i.value for i in at.info)
    # show dismissed -> rows back with who/when, undo one
    at.sidebar.checkbox[-1].check().run()
    _select(at, _table_key(at, "projects_table_"), [0])
    issues = at.dataframe[1].value
    assert len(issues) == 3 and set(issues["dismissed_by"]) != {""}
    assert at.dataframe[0].value.iloc[0]["mapfile"] == "flagged rows (dismissed)"
    _select(at, f"issues_table_P1", [0])
    _button(at, "Undo selected (1)").click().run()
    assert len(dismiss.read(dismiss.default_log_path(p))[0]) == 3   # 4 dismissed, 1 undone

def test_projects_dismiss_unwritable_log_shows_hint(tmp_path, monkeypatch):
    p = _issues_catalog(tmp_path)
    log = tmp_path / ".cat_dismissed.tsv"
    log.write_text("")
    log.chmod(0o444)
    if os.access(str(log), os.W_OK):
        pytest.skip("running as root")
    at = _apptest(monkeypatch, p, "Projects")
    _select(at, _table_key(at, "projects_table_"), [0])
    _button(at, "Dismiss all").click().run()
    assert not at.exception
    assert any("chmod 664" in e.value for e in at.error)

def test_projects_sidebar_reports_unreadable_lines(tmp_path, monkeypatch):
    p = _issues_catalog(tmp_path)
    (tmp_path / ".cat_dismissed.tsv").write_text("junk\n")
    at = _apptest(monkeypatch, p, "Projects")
    assert any("1 unreadable line(s)" in c.value for c in at.sidebar.caption)
```

(`_grab_catalog` names the file `cat.db`, so the default log is `.cat_dismissed.tsv`.)

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest -q tests/test_gui_app.py -k projects_dismiss`
Expected: FAIL (no such button / column text mismatch)

- [ ] **Step 3: Implement** per Behavior above. Add `import getpass` and `from .. import dismiss` to the app imports.

- [ ] **Step 4: Run the suite**

Run: `python -m pytest -q`
Expected: all pass, including `test_stale_selection_after_filter_does_not_crash[Projects]`

- [ ] **Step 5: Commit**

```bash
git add seqledger/app/streamlit_app.py tests/test_gui_app.py
git commit -m "gui: dismiss and undo issues from the Projects view"
```

---

### Task 5: Samples and Files views

**Files:**
- Modify: `seqledger/app/streamlit_app.py` (`samples_view`, `files_view`)
- Test: `tests/test_gui_app.py`

**Interfaces:**
- Consumes: `_dismiss_sidebar`, `_record`, `_tint`, `_severity`, `dismiss.sample_key`, `dismiss.file_key`, `load_files` now carrying `rel_path`.

Behavior:
- Samples: `flags` cell -> `OK *` when `dismiss.sample_key(pid, sid, flags)` in dismissed (`flags + " (dismissed)"` when show); table rendered through `_tint(..., ["flags", "backup"])`. Under the table for the selected sample with live non-empty flags: `st.text_input("Note", key="note_flags")` and `st.button("Dismiss flags for this sample")`; when dismissed and `show_dismissed`: `st.button("Undo")`. Files list under the sample: `integrity` and `backup` cells use the same substitution as the Files view and are tinted.
- Files: for each row compute `integrity_key = file_key(pid, rel_path, {"gzip_error": "gzip error", "format_error": "format error"}[integrity])` when integrity is one of those, or `empty (0 reads)` when integrity `ok` and `n_reads == 0`; `backup_key = file_key(pid, rel_path, "md5 mismatch")` when backup is `Mismatch`. Dismissed -> cell `OK *` (or `<label> (dismissed)` when show). Sidebar `Integrity`/`Backup status` filters match the raw state only for rows that are not dismissed (a dismissed `gzip_error` is not returned by that filter). Table through `_tint(..., ["backup", "integrity"])`. Under the table for the selected file: one `Dismiss <kind>` button per live problem (`Dismiss gzip error`, `Dismiss md5 mismatch`, ...), a shared `Note` input (key `note_file`), and `Undo <kind>` buttons for dismissed problems when show is on.

- [ ] **Step 1: Write the failing tests**

```python
def test_samples_dismiss_flags(tmp_path, monkeypatch):
    from seqledger import dismiss
    p = _issues_catalog(tmp_path)
    con = sqlite3.connect(p); con.execute("UPDATE samples SET flags='na_uniqid' WHERE sample_id='s1'")
    con.commit(); con.close()
    at = _apptest(monkeypatch, p, "Samples")
    key = _table_key(at, "samples_table_")
    i = list(at.dataframe[0].value["sample_id"]).index("s1")
    _select(at, key, [i])
    _button(at, "Dismiss flags for this sample").click().run()
    assert at.dataframe[0].value.iloc[i]["flags"] == "OK *"
    assert dismiss.sample_key("P1", "s1", "na_uniqid") in dismiss.read(dismiss.default_log_path(p))[0]
    at.sidebar.checkbox[-1].check().run()
    assert at.dataframe[0].value.iloc[i]["flags"] == "na_uniqid (dismissed)"

def test_files_dismiss_integrity_and_md5(tmp_path, monkeypatch):
    p = _issues_catalog(tmp_path)
    at = _apptest(monkeypatch, p, "Files")
    key = _table_key(at, "files_table_")
    df = at.dataframe[0].value
    i = list(df["filename"]).index("s2_R1.fastq.gz")   # gzip_error
    _select(at, key, [i])
    _button(at, "Dismiss gzip error").click().run()
    assert at.dataframe[0].value.iloc[i]["integrity"] == "OK *"
    j = list(at.dataframe[0].value["filename"]).index("s2_R2.fastq.gz")   # md5 mismatch
    _select(at, key, [j])
    _button(at, "Dismiss md5 mismatch").click().run()
    assert at.dataframe[0].value.iloc[j]["backup"] == "OK *"
    # the integrity filter no longer returns the dismissed file
    sel = next(s for s in at.sidebar.selectbox if s.label == "Integrity")
    sel.set_value("gzip_error").run()
    assert "s2_R1.fastq.gz" not in list(at.dataframe[0].value["filename"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest -q tests/test_gui_app.py -k "samples_dismiss or files_dismiss"`
Expected: FAIL

- [ ] **Step 3: Implement** per Behavior.

- [ ] **Step 4: Run the suite**

Run: `python -m pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add seqledger/app/streamlit_app.py tests/test_gui_app.py
git commit -m "gui: dismiss sample flags and file problems"
```

---

### Task 6: Grab & Go honours dismissals

**Files:**
- Modify: `seqledger/app/streamlit_app.py` (`custom_table_view`)
- Test: `tests/test_gui_app.py` (`test_grab_and_go_flags_problem_files_and_lane_splits` and one new test)

**Interfaces:**
- Consumes: `_dismiss_state`, `_file_problem(r, dismissed)`, `_files_ok`, `_tint`.

Behavior: `fpaths["problem"]` computed with the dismissed dict; `bad` stays `isin(_PROBLEM_KINDS)` so `dismissed` files are copied, counted in the estimate, and mapped; `files_ok` tinted via `_tint(table[tcols], ["files_ok"])`; the warning text unchanged.

- [ ] **Step 1: Write the failing test**

```python
def test_grab_and_go_includes_files_with_dismissed_problems(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from seqledger import dismiss
    p = _grab_catalog(tmp_path)
    log = dismiss.default_log_path(p)
    dismiss.append(log, "dismiss", dismiss.file_key("P1", "P1/s2_R1.fastq.gz", "gzip error"), "u")
    dismiss.append(log, "dismiss", dismiss.file_key("P1", "P1/s2_R2.fastq.gz", "missing from disk"), "u")
    monkeypatch.setenv("SEQLEDGER_DB", p)
    at = AppTest.from_file(_APP_PATH, default_timeout=30)
    at.session_state["cart"] = {f"P1\t{s}": {"project_id": "P1", "sample_id": s} for s in ("s1", "s2")}
    at.run()
    at.sidebar.radio[0].set_value("Grab & Go").run()
    table = at.dataframe[1].value
    assert table[table["sample_id"] == "s2"].iloc[0]["files_ok"] == "ok *"
    assert not any("files in the cart are corrupt" in w.value for w in at.warning)
    dest = next(t for t in at.text_input if t.label.startswith("Destination"))
    dest.set_value("/pool/dest").run()
    assert any("Script preview (6 files total" in e.label for e in at.expander)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest -q tests/test_gui_app.py -k dismissed_problems`
Expected: FAIL (`files_ok` is `1 corrupt, 1 missing`)

- [ ] **Step 3: Implement** per Behavior. The existing `test_grab_and_go_flags_problem_files_and_lane_splits` must still pass unchanged (no dismissals there).

- [ ] **Step 4: Run the suite**

Run: `python -m pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add seqledger/app/streamlit_app.py tests/test_gui_app.py
git commit -m "gui: Grab & Go copies files whose problems were dismissed"
```

---

### Task 7: Documentation and changelog

**Files:**
- Modify: `docs/user-guide.md` (new `### Dismissing issues` section after `## What the status columns mean`, and a line under each view mentioning the colors)
- Modify: `docs/admin-guide.md` (`## Permissions and privacy`: the log file; `## Hosting the GUI` Scratch paragraph: the `dismiss_log` key)
- Modify: `docs/reference/statuses.md` (one paragraph: `OK *` and `(dismissed)` labels, tints)
- Modify: `CHANGELOG.md` (`## [Unreleased]`, `### Added`)

Content to cover (exact facts, wording free):
- User guide: any user can dismiss; what it hides and does not change; the three scopes and where the buttons are; `OK *`, ` *` on partly dismissed cells, `Show dismissed`, Undo; notes; a dismissed file goes into Grab & Go copies; the color key: vermillion error, orange warning, blue dismissed, grey not yet checked, no color OK.
- Admin guide: file `.<catalog>_dismissed.tsv` next to the catalog, created on first dismiss with mode 664; to pre-create: `touch <path> && chmod 664 <path>` (or 666 without a shared group); plain text, safe to edit or truncate, last line per item wins; `init-db` stores its path in `dismiss_log` so Scratch copies use the master's log; on nodes without Store the GUI shows dismissals as unavailable.
- Changelog, Added: one bullet for dismissals (log file, toggle, `OK *`), one for the tinted status cells, one noting `init-db` now records `dismiss_log`. Remind users to re-run `init-db` on the master so copies find the log.

- [ ] **Step 1: Write the docs and changelog**
- [ ] **Step 2: Check no em dashes were introduced**

Run: `grep -n $'\xe2\x80\x94' docs/user-guide.md docs/admin-guide.md docs/reference/statuses.md CHANGELOG.md | grep -v "^.*:[0-9]*:.*Catalog:" ; echo done`
Expected: no new lines from the edited sections (pre-existing ones elsewhere are fine).

- [ ] **Step 3: Run the suite once more**

Run: `python -m pytest -q`
Expected: all pass

- [ ] **Step 4: Commit**

```bash
git add docs CHANGELOG.md
git commit -m "docs: dismissing GUI issues"
```

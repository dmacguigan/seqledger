"""Gzip integrity checking for cataloged FASTQ files.

Bulk-decompresses each *.fastq.gz to EOF (stdlib gzip == `gzip -t`, catching
truncation / CRC / bit-rot -- i.e. "reads cleanly and is not corrupt"), counts
reads from a C-level newline tally, and compares R1/R2 read counts per sample
(parity). Results are persisted to the `files` table and summarized per project
into `validation_log`. This is deliberately not FastQC: it verifies the data is
readable and intact without the (slower) per-read analysis, and keeps no reports.

Decompression releases the GIL, so files are checked concurrently with a
ThreadPoolExecutor; all DB writes happen on the calling thread. Stdlib-only by
default, transparently using python-isal (ISA-L) for faster decode when present.

Incremental + resumable: files that already passed and are unchanged on disk
(same size) are skipped without re-reading, and results are committed as the run
proceeds, so re-runs are cheap and an interrupted run resumes where it stopped.
The dominant cost is reading every byte over the (often network-mounted) storage,
so skipping unchanged files -- not a faster codec -- is the main speedup.
"""

import glob
import gzip
import json
import os
import zlib
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import date

# Prefer ISA-L (python-isal) for decompression when installed: a drop-in for
# gzip.open that decodes ~2x faster and raises on the same corruption cases.
# Falls back to stdlib gzip (which on Python 3.14+ is itself zlib-ng-backed).
# This only helps when the check is CPU-bound; on a network mount the read
# dominates and the incremental skip below is what actually saves time.
_GZIP_ERRORS = (gzip.BadGzipFile, EOFError, zlib.error, OSError)
try:
    from isal import igzip as _fastgz, isal_zlib as _isal_zlib
    _GZIP_ERRORS = _GZIP_ERRORS + (_isal_zlib.error,)
except ImportError:
    _fastgz = gzip

# Persist accumulated per-file results (and commit) every this many checks, so a
# long run is resumable: a kill mid-run keeps everything already checked.
_COMMIT_EVERY = 200

# Per-file integrity_status values.
OK = "ok"
GZIP_ERROR = "gzip_error"      # invalid / truncated gzip, CRC failure
FORMAT_ERROR = "format_error"  # decompresses but not valid FASTQ
UNCHECKED = "unchecked"        # file not found on disk / no root known


def check_fastq_gz(path, chunk_size=1 << 20):
    """Verify one .fastq.gz reads cleanly and is not corrupt.

    Bulk-decompresses the stream in binary chunks to EOF -- this is the `gzip -t`
    check (any truncation / CRC / bit-rot surfaces as a decode error) and is
    ~30x cheaper than iterating lines in Python, which throttled throughput. Read
    count comes from a C-level newline tally (bytes.count), and a line count that
    is not a multiple of 4 is flagged as a structural (format) error.

    Returns {"status", "n_reads", "detail"}. Note: this validates that the data
    decompresses intact; it does not check per-record '@'/'+' framing.
    """
    n_lines = 0
    ended_nl = True
    try:
        with _fastgz.open(path, "rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                n_lines += chunk.count(b"\n")
                ended_nl = chunk.endswith(b"\n")
    except _GZIP_ERRORS as e:
        return {"status": GZIP_ERROR, "n_reads": None, "detail": str(e)}

    if not ended_nl:  # final record without a trailing newline still counts
        n_lines += 1
    if n_lines % 4 != 0:
        return {"status": FORMAT_ERROR, "n_reads": None,
                "detail": f"line count {n_lines} is not a multiple of 4"}
    return {"status": OK, "n_reads": n_lines // 4, "detail": None}


def _check_path(path, cached=None, recheck=False):
    """Existence + integrity for one path (runs in a worker thread).

    Does a single os.stat (one network round-trip, parallelized across workers).
    Missing files are 'unchecked'. When the file previously passed (cached gz_ok=1)
    and its on-disk size still matches the recorded size_bytes, it is skipped
    without re-reading -- the expensive full decompress is replaced by one stat.
    Pass recheck=True to force a full re-read regardless of cached state.

    The returned dict carries "cached": True for a skip (its status/n_reads come
    straight from the prior run), else the fresh check_fastq_gz result.
    """
    try:
        size = os.stat(path).st_size
    except OSError:
        return {"status": UNCHECKED, "n_reads": None, "detail": "not found on disk",
                "cached": False}
    if (not recheck and cached and cached.get("gz_ok") == 1
            and cached.get("integrity_date") and cached.get("size_bytes") is not None
            and size == cached["size_bytes"]):
        return {"status": OK, "n_reads": cached.get("n_reads"), "detail": None,
                "cached": True}
    res = check_fastq_gz(path)
    res["cached"] = False
    return res


def _resolve_path(seqdata_root, row):
    """Absolute on-disk path for a files row, or None if no root is known."""
    root = seqdata_root or row["seqdata_root"]
    if not root or not row["rel_path"]:
        return None
    return os.path.join(root, row["rel_path"])


def _persist(conn, file_pk, res, today):
    gz_ok = None if res["status"] == UNCHECKED else (1 if res["status"] == OK else 0)
    conn.execute(
        """UPDATE files SET integrity_status=?, gz_ok=?, n_reads=?, integrity_date=?
           WHERE file_pk=?""",
        (res["status"], gz_ok, res.get("n_reads"), today, file_pk))


def _log_run(conn, project_id, status, today):
    conn.execute(
        "INSERT INTO validation_log (project_id, run_date, status) VALUES (?,?,?)",
        (project_id, today, status))


def _select_rows(conn, only_project=None):
    """Files joined to their project root, optionally limited to one project_id."""
    sql = ("SELECT f.file_pk, f.project_id, f.sample_pk, f.direction, f.filename, "
           "f.rel_path, f.size_bytes, f.gz_ok, f.n_reads, f.integrity_date, "
           "p.seqdata_root "
           "FROM files f JOIN projects p ON p.project_id = f.project_id")
    params = ()
    if only_project:
        sql += " WHERE f.project_id = ?"
        params = (only_project,)
    sql += " ORDER BY f.project_id"
    return conn.execute(sql, params).fetchall()


def _cached_state(rows):
    """Prior per-file state, so a worker can skip an unchanged, already-passed file."""
    return {r["file_pk"]: {"size_bytes": r["size_bytes"], "gz_ok": r["gz_ok"],
                           "n_reads": r["n_reads"], "integrity_date": r["integrity_date"]}
            for r in rows}


def _run_checks(to_check, cached, jobs, progress, recheck, on_checked=None):
    """Concurrently check a {file_pk: path} map; return {file_pk: result}.

    on_checked(pk, res), if given, is called on the calling thread for each freshly
    checked (non-skipped) file -- used to persist + commit incrementally so a kill
    mid-run keeps completed work.
    """
    results = {}
    total = len(to_check)
    if not total:
        return results
    if progress:
        print(f"checking {total} cataloged file(s) on disk "
              "(reading every byte of new/changed files; skipping unchanged "
              "ones that already passed) ...", flush=True)
    checked = skipped = 0
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futures = {ex.submit(_check_path, p, cached.get(pk), recheck): pk
                   for pk, p in to_check.items()}
        pending = set(futures)
        start = time.monotonic()
        # Poll with a timeout so a heartbeat (with elapsed time) prints every few
        # seconds even while the first large file is still being read -- otherwise
        # there is no output until the first whole file completes.
        while pending:
            done, pending = wait(pending, timeout=5, return_when=FIRST_COMPLETED)
            for fut in done:
                pk = futures[fut]
                res = fut.result()
                results[pk] = res
                if res.get("cached"):
                    skipped += 1
                else:
                    checked += 1
                    if on_checked:
                        on_checked(pk, res)
            if progress:
                print(f"\r  checked {checked}, skipped {skipped}, "
                      f"{checked + skipped}/{total} files "
                      f"({int(time.monotonic() - start)}s)   ", end="", flush=True)
    if progress:
        print()
    return results


def _compute_results(rows, seqdata_root, jobs, progress, recheck, on_checked=None):
    """Resolve paths and check rows; unresolved (no root) paths become UNCHECKED.

    Returns {file_pk: result}. Does no DB writes itself; pass on_checked to persist.
    """
    cached = _cached_state(rows)
    # Resolve paths on the calling thread. The on-disk stat is done inside the
    # workers (not a serial pass here): over thousands of files on a network mount
    # each stat() is a round-trip, and doing them serially would hang silently for
    # minutes before the first progress tick.
    paths = {r["file_pk"]: _resolve_path(seqdata_root, r) for r in rows}
    to_check = {pk: p for pk, p in paths.items() if p}
    results = _run_checks(to_check, cached, jobs, progress, recheck, on_checked)
    # Files with no resolvable path (root unknown) -> unchecked.
    for pk in paths:
        if pk not in results:
            results[pk] = {"status": UNCHECKED, "n_reads": None, "detail": None,
                           "cached": False}
    if progress and results and all(r["status"] == UNCHECKED for r in results.values()):
        print("  note: all files came back 'unchecked' -- none were found on disk "
              "(is --seqdata-root correct, and are the files mounted?)")
    return results


def aggregate_from_db(conn, project_ids, log=True):
    """Summarize per-project integrity + R1/R2 parity by reading the files table.

    Reads persisted integrity_status/n_reads straight from the DB, so it works the
    same whether results were written by a live run or merged from batch JSON.
    Writes a per-project validation_log row when log=True. Returns
    {project_id: summary_dict}.
    """
    today = date.today().isoformat()
    summaries = {}
    for pid in project_ids:
        rows = conn.execute(
            "SELECT integrity_status, n_reads, direction, sample_pk "
            "FROM files WHERE project_id=?", (pid,)).fetchall()
        s = {"n_files": 0, "n_ok": 0, "n_gzip_error": 0, "n_format_error": 0,
             "n_unchecked": 0, "parity_warnings": []}
        mates = {}  # sample_pk -> {direction: n_reads}
        for r in rows:
            status = r["integrity_status"] or UNCHECKED
            s["n_files"] += 1
            s[{OK: "n_ok", GZIP_ERROR: "n_gzip_error", FORMAT_ERROR: "n_format_error",
               UNCHECKED: "n_unchecked"}.get(status, "n_unchecked")] += 1
            if r["sample_pk"] is not None and status == OK:
                mates.setdefault(r["sample_pk"], {})[r["direction"]] = r["n_reads"]
        for sample_pk, rc in mates.items():
            if (rc.get("R1") is not None and rc.get("R2") is not None
                    and rc["R1"] != rc["R2"]):
                s["parity_warnings"].append(
                    f"sample_pk={sample_pk}: R1 has {rc['R1']} reads, R2 has {rc['R2']}")
        if s["n_gzip_error"] or s["n_format_error"]:
            s["status"] = "fail"
        elif s["parity_warnings"] or s["n_unchecked"]:
            s["status"] = "warn"
        else:
            s["status"] = "pass"
        if log:
            _log_run(conn, pid, s["status"], today)
        summaries[pid] = s
    conn.commit()
    return summaries


def list_projects(conn, only_project=None):
    """Project_ids that have cataloged files (optionally just the one requested)."""
    return sorted({r["project_id"] for r in _select_rows(conn, only_project)})


def check_catalog_integrity(conn, seqdata_root=None, only_project=None, jobs=None,
                            progress=True, recheck=False):
    """Check gzip/FASTQ integrity for cataloged files and persist results.

    seqdata_root overrides the per-project stored root (else projects.seqdata_root
    is used). only_project limits to one project_id. jobs sets the worker count
    (default min(8, cpu count); on a network mount this is a *stream* count, not a
    CPU count -- raising it can fill a pipe a single stream can't).

    Incremental by default: a file that previously passed (gz_ok=1) and whose
    on-disk size is unchanged is skipped without re-reading its bytes, so re-runs
    and resumed runs are cheap. Results are persisted and committed every
    _COMMIT_EVERY files, so a kill mid-run keeps all completed work (the next run
    resumes from where it stopped). Pass recheck=True to force a full re-read of
    every file. With progress=True, prints a live checked/skipped counter.
    Returns {project_id: summary_dict}.
    """
    if jobs is None:
        jobs = min(8, os.cpu_count() or 1)
    today = date.today().isoformat()
    rows = _select_rows(conn, only_project)

    since_commit = [0]

    def on_checked(pk, res):
        # Persist as we go so a kill mid-run keeps completed work.
        _persist(conn, pk, res, today)
        since_commit[0] += 1
        if since_commit[0] >= _COMMIT_EVERY:
            conn.commit()
            since_commit[0] = 0

    results = _compute_results(rows, seqdata_root, jobs, progress, recheck, on_checked)
    # Persist no-path UNCHECKED files (never entered the worker pool). Cached rows
    # are already correct in the DB; re-persisting an UNCHECKED row is idempotent.
    for pk, res in results.items():
        if res["status"] == UNCHECKED and not res.get("cached"):
            _persist(conn, pk, res, today)
    conn.commit()

    return aggregate_from_db(conn, sorted({r["project_id"] for r in rows}))


def emit_project_json(conn, project_id, out_path, seqdata_root=None, jobs=None,
                      progress=True, recheck=False):
    """Check one project and write per-file results to JSON -- no DB writes.

    Intended for a remote (qsub) worker that must not write the shared catalog DB
    concurrently: it reads the project's file list from the DB, checks the files on
    the storage it can reach, and emits
    {project_id, run_date, results: {file_pk: {status, n_reads}}}.
    Merge the JSON back into the catalog later with collect_json (integrity
    --collect). The incremental skip still applies -- a re-submitted job reads the
    prior gz_ok/size from the DB rows and skips unchanged files that already passed.
    """
    if jobs is None:
        jobs = min(8, os.cpu_count() or 1)
    rows = _select_rows(conn, project_id)
    results = _compute_results(rows, seqdata_root, jobs, progress, recheck)
    payload = {"project_id": project_id, "run_date": date.today().isoformat(),
               "results": {str(pk): {"status": r["status"], "n_reads": r["n_reads"]}
                           for pk, r in results.items()}}
    tmp = out_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, out_path)  # atomic: a reader never sees a half-written file
    if progress:
        print(f"wrote {len(results)} file result(s) for {project_id} -> {out_path}")
    return payload


def collect_json(conn, results_dir, progress=True):
    """Merge batch integrity JSON files from results_dir into the catalog DB.

    Persists each file's status/n_reads (using the emitting job's run_date), then
    re-aggregates per-project summaries and writes validation_log. This is the only
    step that writes the shared DB, so it runs locally and serially -- the remote
    jobs never touch it. Returns {project_id: summary_dict}.
    """
    paths = sorted(glob.glob(os.path.join(results_dir, "*.json")))
    if not paths:
        if progress:
            print(f"no *.json result files in {results_dir}")
        return {}
    today = date.today().isoformat()
    project_ids = set()
    n_files = 0
    for path in paths:
        with open(path) as fh:
            payload = json.load(fh)
        pid = payload.get("project_id")
        run_date = payload.get("run_date") or today
        pfiles = payload.get("results", {})
        for pk_str, r in pfiles.items():
            _persist(conn, int(pk_str),
                     {"status": r["status"], "n_reads": r.get("n_reads")}, run_date)
            n_files += 1
        if pid:
            project_ids.add(pid)
        if progress:
            print(f"merged {path} ({pid}, {len(pfiles)} files)")
    conn.commit()
    if progress:
        print(f"persisted {n_files} file result(s) from {len(paths)} project file(s)")
    return aggregate_from_db(conn, sorted(project_ids))

"""Gzip + FASTQ structural integrity checking for cataloged FASTQ files.

Stream-decompresses each *.fastq.gz to EOF (stdlib gzip == `gzip -t`, catching
truncation / CRC / bit-rot) while validating the FASTQ 4-line record structure,
then compares R1/R2 read counts per sample (parity). Results are persisted to
the `files` table and summarized per project into `validation_log`.

Stdlib only. Decompression releases the GIL, so files are checked concurrently
with a ThreadPoolExecutor; all DB writes happen on the calling thread.
"""

import gzip
import os
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

# Per-file integrity_status values.
OK = "ok"
GZIP_ERROR = "gzip_error"      # invalid / truncated gzip, CRC failure
FORMAT_ERROR = "format_error"  # decompresses but not valid FASTQ
UNCHECKED = "unchecked"        # file not found on disk / no root known


def check_fastq_gz(path):
    """Stream-decompress one .fastq.gz, validating gzip + FASTQ structure.

    Returns {"status", "n_reads", "detail"}. A single pass validates both the
    gzip stream (errors surface as the trailer/CRC is read at EOF) and the
    FASTQ record structure (every 1st line starts '@', every 3rd starts '+',
    total lines divisible by 4).
    """
    n_lines = 0
    bad_format = None
    try:
        with gzip.open(path, "rb") as fh:
            for line in fh:
                pos = n_lines % 4
                if bad_format is None:
                    if pos == 0 and not line.startswith(b"@"):
                        bad_format = f"line {n_lines + 1}: header does not start with '@'"
                    elif pos == 2 and not line.startswith(b"+"):
                        bad_format = f"line {n_lines + 1}: separator does not start with '+'"
                n_lines += 1
    except (gzip.BadGzipFile, EOFError, zlib.error, OSError) as e:
        return {"status": GZIP_ERROR, "n_reads": None, "detail": str(e)}

    if bad_format is not None:
        return {"status": FORMAT_ERROR, "n_reads": None, "detail": bad_format}
    if n_lines % 4 != 0:
        return {"status": FORMAT_ERROR, "n_reads": None,
                "detail": f"line count {n_lines} is not a multiple of 4"}
    return {"status": OK, "n_reads": n_lines // 4, "detail": None}


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


def check_catalog_integrity(conn, seqdata_root=None, only_project=None, jobs=None,
                            progress=True):
    """Check gzip/FASTQ integrity for cataloged files and persist results.

    seqdata_root overrides the per-project stored root (else projects.seqdata_root
    is used). only_project limits to one project_id. jobs sets the worker count
    (default min(8, cpu count)). With progress=True, prints a live 'checked
    i/total files' counter (this reads every byte, so it can take a while).
    Returns {project_id: summary_dict}.
    """
    if jobs is None:
        jobs = min(8, os.cpu_count() or 1)
    today = date.today().isoformat()

    sql = ("SELECT f.file_pk, f.project_id, f.sample_pk, f.role, f.filename, "
           "f.rel_path, p.seqdata_root "
           "FROM files f JOIN projects p ON p.project_id = f.project_id")
    params = ()
    if only_project:
        sql += " WHERE f.project_id = ?"
        params = (only_project,)
    sql += " ORDER BY f.project_id"
    rows = conn.execute(sql, params).fetchall()

    # Resolve paths on the calling thread; check present files concurrently.
    if progress:
        print(f"scanning {len(rows)} cataloged file(s) on disk ...", flush=True)
    paths = {r["file_pk"]: _resolve_path(seqdata_root, r) for r in rows}
    to_check = {pk: p for pk, p in paths.items() if p and os.path.isfile(p)}

    results = {}  # file_pk -> result dict
    if to_check:
        total = len(to_check)
        step = 1 if total <= 50 else max(1, total // 100)
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futures = {ex.submit(check_fastq_gz, p): pk for pk, p in to_check.items()}
            for i, fut in enumerate(as_completed(futures), 1):
                results[futures[fut]] = fut.result()
                if progress and (i == 1 or i % step == 0 or i == total):
                    print(f"\r  checked {i}/{total} files", end="", flush=True)
        if progress:
            print()
    elif progress:
        print("  no cataloged files found on disk to check "
              "(is --seqdata-root correct, and are the files mounted?)")
    for pk in paths:
        results.setdefault(pk, {"status": UNCHECKED, "n_reads": None, "detail": None})

    # Persist per-file results.
    for pk, res in results.items():
        _persist(conn, pk, res, today)

    # Aggregate per project + R1/R2 read-count parity per sample.
    summaries = {}
    mates = {}  # (project_id, sample_pk) -> {role: n_reads}
    for r in rows:
        pid = r["project_id"]
        res = results[r["file_pk"]]
        s = summaries.setdefault(pid, {
            "n_files": 0, "n_ok": 0, "n_gzip_error": 0, "n_format_error": 0,
            "n_unchecked": 0, "parity_warnings": []})
        s["n_files"] += 1
        s[{OK: "n_ok", GZIP_ERROR: "n_gzip_error", FORMAT_ERROR: "n_format_error",
           UNCHECKED: "n_unchecked"}[res["status"]]] += 1
        if r["sample_pk"] is not None and res["status"] == OK:
            mates.setdefault((pid, r["sample_pk"]), {})[r["role"]] = res["n_reads"]

    for (pid, _sample_pk), rc in mates.items():
        if rc.get("R1") is not None and rc.get("R2") is not None and rc["R1"] != rc["R2"]:
            summaries[pid]["parity_warnings"].append(
                f"sample_pk={_sample_pk}: R1 has {rc['R1']} reads, R2 has {rc['R2']}")

    # Log a per-project run status.
    for pid, s in summaries.items():
        if s["n_gzip_error"] or s["n_format_error"]:
            status = "fail"
        elif s["parity_warnings"] or s["n_unchecked"]:
            status = "warn"
        else:
            status = "pass"
        s["status"] = status
        _log_run(conn, pid, status, today)

    conn.commit()
    return summaries

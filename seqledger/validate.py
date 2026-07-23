"""Validation of map file metadata against sequence data.

Rules are split into FAIL (reject ingest) and WARN (record, proceed). Matching
between metadata and files is done by EXACT basename, both directions, fixing the
substring bug in the old validate_seq_data.py.
"""

import glob
import os
from datetime import date

from .db import METADATA_SUFFIX, fastq_globs, get_config, header_uniqid_column

FAIL = "FAIL"
WARN = "WARN"


class Finding:
    def __init__(self, level, message):
        self.level = level
        self.message = message

    def __repr__(self):
        return f"{self.level}: {self.message}"


# The value written when a non-key field (Taxon / UniqID) is empty in the mapfile.
NA_VALUE = "NA"

# Per-sample quality flags recorded on samples.flags, with a plain-english message.
FLAG_MESSAGES = {
    "na_taxon":   "Taxon was empty in the mapfile, set to NA",
    "na_uniqid":  "UniqID was empty in the mapfile, set to NA",
    "missing_r1": "R1 filename was empty (no R1 file cataloged)",
    "missing_r2": "R2 filename was empty (no R2 file cataloged)",
    "r1_eq_r2":   "R1 and R2 name the same file (only R1 cataloged)",
}


def plan_rows(header, rows):
    """Decide, per mapfile row, whether to load it and how to repair missing values.

    Instead of failing a whole project on a bad row, we load what we can and flag
    the rest. For each row returns a plan dict:
      load        -- True to load the sample, False to skip it
      skip_reason -- why it was skipped (only when load is False)
      line        -- 1-based mapfile line (header = line 1)
      sample_id/r1/r2/taxon/uniq_id -- repaired values (empty Taxon/UniqID -> 'NA';
                     an empty R1/R2 stays '' meaning "no file for that direction")
      flags       -- list of flag tokens (see FLAG_MESSAGES) describing what was repaired
      extra       -- the original row (for the extra-columns JSON)

    Rows are SKIPPED only when they can't be keyed or would violate uniqueness: an
    empty ID, or a sample ID already used earlier in this mapfile. Returns
    (uniqid_col, plans); uniqid_col is None if the header is invalid.
    """
    uniqid_col = header_uniqid_column(header)
    if uniqid_col is None:
        return None, []

    # Header is validated positionally (case/space-insensitive) as ID,R1,R2,Taxon
    # then the UniqID column; rows are keyed by the verbatim header cells, so resolve
    # each logical column to its real header key instead of assuming canonical casing
    # (a lowercase 'id,r1,...' header would otherwise load zero rows).
    id_k, r1_k, r2_k, taxon_k = header[0], header[1], header[2], header[3]

    seen = set()
    plans = []
    for i, row in enumerate(rows, start=2):  # line 2 = first data row
        sample_id = (row.get(id_k) or "").strip()
        r1 = (row.get(r1_k) or "").strip()
        r2 = (row.get(r2_k) or "").strip()
        taxon = (row.get(taxon_k) or "").strip()
        uniq_id = (row.get(uniqid_col) or "").strip()

        if not sample_id:
            plans.append({"load": False, "line": i, "skip_reason": "empty ID"})
            continue
        if sample_id in seen:
            plans.append({"load": False, "line": i,
                          "skip_reason": f"duplicate sample ID '{sample_id}'"})
            continue
        seen.add(sample_id)

        flags = []
        if not taxon:
            taxon = NA_VALUE
            flags.append("na_taxon")
        if not uniq_id:
            uniq_id = NA_VALUE
            flags.append("na_uniqid")
        if not r1:
            flags.append("missing_r1")
        if not r2:
            flags.append("missing_r2")
        if r1 and r2 and r1 == r2:
            flags.append("r1_eq_r2")

        plans.append({"load": True, "line": i, "sample_id": sample_id,
                      "r1": r1, "r2": r2, "taxon": taxon, "uniq_id": uniq_id,
                      "flags": flags, "extra": row})
    return uniqid_col, plans


def validate_metadata(metadata_filename, header, rows, disk_filenames=None,
                      known_uniq_ids=None):
    """Validate one project's map file (produces display findings, no hard content fails).

    A malformed name/header is still a hard FAIL (has_fail True). Row-content
    problems no longer fail the project: empty Taxon/UniqID are NA-filled, empty/dup
    IDs skip just that row -- all surfaced as WARN findings (see plan_rows for the
    loading policy). Returns (findings, has_fail); has_fail is True only for the
    structural suffix/header problems.
    """
    findings = []

    if not metadata_filename.endswith(METADATA_SUFFIX):
        findings.append(Finding(FAIL, f"{metadata_filename} does not end in {METADATA_SUFFIX}"))

    uniqid_col = header_uniqid_column(header)
    if uniqid_col is None:
        findings.append(Finding(
            FAIL,
            "first five columns must be ID,R1,R2,Taxon,UniqID (or UniqueID); "
            f"got {','.join(header[:5])}"))
        return findings, True  # can't check rows without a valid header

    _uniqid_col, plans = plan_rows(header, rows)
    metadata_r1r2 = set()
    for p in plans:
        i = p["line"]
        if not p["load"]:
            findings.append(Finding(WARN, f"row {i}: skipped ({p['skip_reason']})"))
            continue
        for fl in p["flags"]:
            findings.append(Finding(WARN, f"row {i}: {FLAG_MESSAGES[fl]}"))
        for direction, fn in (("R1", p["r1"]), ("R2", p["r2"])):
            if fn:
                metadata_r1r2.add(fn)
                if disk_filenames is not None and fn not in disk_filenames:
                    findings.append(Finding(WARN, f"row {i}: {direction} '{fn}' not found on disk"))
        if known_uniq_ids and p["uniq_id"] != NA_VALUE and p["uniq_id"] in known_uniq_ids:
            findings.append(Finding(
                WARN,
                f"row {i}: UniqID '{p['uniq_id']}' already cataloged under project "
                f"'{known_uniq_ids[p['uniq_id']]}' (possible re-sequence)"))

    # Orphans: fastq on disk not referenced by any metadata row (exact match).
    if disk_filenames is not None:
        for fn in sorted(disk_filenames):
            if fn not in metadata_r1r2:
                findings.append(Finding(WARN, f"'{fn}' on disk is not referenced in metadata"))

    has_fail = any(f.level == FAIL for f in findings)
    return findings, has_fail


def overall_status(findings):
    if any(f.level == FAIL for f in findings):
        return "fail"
    if findings:
        return "warn"
    return "pass"


def check_data_files(conn, project_id, seq_data_relpath, seqdata_root):
    """Reciprocal mapfile <-> disk check for one project.

    Returns dict: status ('ok'|'issues'|'unchecked'), n_missing, n_orphan,
    missing (mapfile R1/R2 not on disk), orphan (disk fastq.gz not in mapfile).
    'unchecked' when the data dir is unreachable. When seqdata_root is None it
    falls back to the project's stored projects.seqdata_root (matching integrity),
    so each project scans under the root it was ingested from.
    """
    if seqdata_root is None:
        row = conn.execute(
            "SELECT seqdata_root FROM projects WHERE project_id=?", (project_id,)).fetchone()
        seqdata_root = row["seqdata_root"] if row else None
    # Compare by REL_PATH (physical identity), not basename: two files can share a
    # basename in different subdirs, and the DB now keys files on rel_path.
    db_files = {r["rel_path"] for r in conn.execute(
        "SELECT rel_path FROM files WHERE project_id=?", (project_id,))}
    if not seqdata_root or not seq_data_relpath:
        return {"status": "unchecked", "n_missing": None, "n_orphan": None,
                "missing": [], "orphan": []}
    data_dir = os.path.join(seqdata_root, seq_data_relpath)
    if not os.path.isdir(data_dir):
        return {"status": "unchecked", "n_missing": None, "n_orphan": None,
                "missing": [], "orphan": []}
    # Recursive: FASTQ may be nested in subdirs (matches ingest's discovery), and
    # the extension set comes from the catalog config (default fastq.gz + fq.gz).
    # Disk paths are made relative to seqdata_root, the same convention as files.rel_path.
    globs = fastq_globs(get_config(conn, "fastq_extensions")) or ["*.fastq.gz", "*.fq.gz"]
    disk = set()
    for pat in globs:
        disk.update(os.path.relpath(p, seqdata_root)
                    for p in glob.glob(os.path.join(data_dir, "**", pat), recursive=True))
    missing = sorted(db_files - disk)
    orphan = sorted(disk - db_files)
    status = "ok" if not missing and not orphan else "issues"
    return {"status": status, "n_missing": len(missing), "n_orphan": len(orphan),
            "missing": missing, "orphan": orphan}


def check_checksums(conn, project_id):
    """Store vs P-drive md5 status for one project, from stored md5_match.

    Returns dict: status ('verified'|'mismatch'|'incomplete'|'empty'),
    n_files, n_mismatch, n_uncompared.
    """
    r = conn.execute(
        """SELECT COUNT(*) AS n,
                  SUM(CASE WHEN md5_match=0 THEN 1 ELSE 0 END) AS n_mismatch,
                  SUM(CASE WHEN md5_match IS NULL THEN 1 ELSE 0 END) AS n_uncompared
           FROM files WHERE project_id=?""", (project_id,)).fetchone()
    n = r["n"] or 0
    n_mismatch = r["n_mismatch"] or 0
    n_uncompared = r["n_uncompared"] or 0
    if n == 0:
        status = "empty"
    elif n_mismatch > 0:
        status = "mismatch"
    elif n_uncompared > 0:
        status = "incomplete"
    else:
        status = "verified"
    return {"status": status, "n_files": n, "n_mismatch": n_mismatch,
            "n_uncompared": n_uncompared}


def _persist_data_check_issues(conn, project_id, missing, orphan):
    """Rewrite the project's data-files issue rows (missing + orphan filenames)."""
    conn.execute("DELETE FROM data_check_issues WHERE project_id=?", (project_id,))
    conn.executemany(
        "INSERT INTO data_check_issues(project_id, kind, filename) VALUES (?,?,?)",
        [(project_id, "missing from disk", fn) for fn in missing]
        + [(project_id, "missing from mapfile", fn) for fn in orphan])


def validate_catalog(conn, seqdata_root=None):
    """Run both per-project checks over the whole catalog.

    Data-files reciprocal check runs (and is persisted to `projects`) only when
    seqdata_root is given; otherwise the last stored data-check result is read
    back. The checksum check always runs from stored md5s. UniqIDs shared across
    projects are surfaced as extra notes.

    Returns {project_id: {"data": {...}, "checksum": {...}, "notes": [Finding]}}.
    """
    projects = conn.execute(
        "SELECT project_id, seq_data_relpath, seqdata_root FROM projects ORDER BY project_id"
    ).fetchall()

    # UniqIDs used in more than one distinct project (excluding NULL/''/'NA'
    # placeholders). Aggregate the (uniq_id, project_id) rows in Python rather than
    # GROUP_CONCAT + split(',') -- the comma separator is ambiguous when a project_id
    # itself contains a comma, which would mis-attribute the shared IDs.
    uniq_projects = {}
    for r in conn.execute(
        """SELECT DISTINCT uniq_id, project_id FROM samples
           WHERE uniq_id IS NOT NULL AND uniq_id != '' AND uniq_id != ?
           ORDER BY uniq_id, project_id""", (NA_VALUE,)):
        uniq_projects.setdefault(r["uniq_id"], []).append(r["project_id"])
    shared = {}
    for uniq_id, projs in uniq_projects.items():
        if len(projs) < 2:
            continue
        joined = ",".join(projs)
        for project_id in projs:
            shared.setdefault(project_id, []).append((uniq_id, joined))

    # Phase 1 -- read only. Do every project's disk scan / checksum read / notes with
    # no write transaction open, so the (network) recursive globs never run while a
    # write lock is held. Scanned results to persist are collected for phase 2.
    today = date.today().isoformat()
    results = {}
    to_persist = []
    for p in projects:
        project_id = p["project_id"]

        # One --seqdata-root overrides all; otherwise each project scans under the
        # root it was ingested from (consistent with integrity.py).
        root = seqdata_root or p["seqdata_root"]
        if root:
            data = check_data_files(conn, project_id, p["seq_data_relpath"], root)
            if data["status"] != "unchecked":
                to_persist.append((project_id, data))
        else:
            row = conn.execute(
                """SELECT data_check_status, data_check_n_missing, data_check_n_orphan
                   FROM projects WHERE project_id=?""", (project_id,)).fetchone()
            data = {"status": row["data_check_status"] or "unchecked",
                    "n_missing": row["data_check_n_missing"],
                    "n_orphan": row["data_check_n_orphan"], "missing": [], "orphan": []}

        checksum = check_checksums(conn, project_id)

        notes = []
        for fn in data["missing"]:
            notes.append(Finding(WARN, f"mapfile file '{fn}' not found on disk"))
        for fn in data["orphan"]:
            notes.append(Finding(WARN, f"disk file '{fn}' not referenced in mapfile"))
        for uniq_id, projs in shared.get(project_id, []):
            notes.append(Finding(WARN, f"UniqID '{uniq_id}' shared across projects: {projs}"))

        results[project_id] = {"data": data, "checksum": checksum, "notes": notes}

    # Phase 2 -- write. Persist each scanned project and commit per project, so the
    # write lock is only ever held briefly, never across the disk scans above.
    for project_id, data in to_persist:
        conn.execute(
            """UPDATE projects SET data_check_status=?, data_check_n_missing=?,
                 data_check_n_orphan=?, data_check_date=? WHERE project_id=?""",
            (data["status"], data["n_missing"], data["n_orphan"], today, project_id))
        _persist_data_check_issues(conn, project_id, data["missing"], data["orphan"])
        conn.commit()

    return results

"""Validation of map file metadata against sequence data.

Rules are split into FAIL (reject ingest) and WARN (record, proceed). Matching
between metadata and files is done by EXACT basename, both directions, fixing the
substring bug in the old validate_seq_data.py.
"""

import glob
import os
from datetime import date

from .db import METADATA_SUFFIX, header_uniqid_column

FAIL = "FAIL"
WARN = "WARN"


class Finding:
    def __init__(self, level, message):
        self.level = level
        self.message = message

    def __repr__(self):
        return f"{self.level}: {self.message}"


def validate_metadata(metadata_filename, header, rows, disk_filenames=None,
                      known_uniq_ids=None):
    """Validate one project's map file.

    - metadata_filename: the map file name (checked for _mapfile.csv suffix).
    - header: list of column names.
    - rows: list of dict-like rows (keys ID, R1, R2, Taxon, and the UniqID column).
    - disk_filenames: optional set of *.fastq.gz basenames present on disk. When
      provided, orphan / missing-file checks run.
    - known_uniq_ids: optional dict {uniq_id: project_id} of already-cataloged IDs,
      for cross-project duplicate detection.

    Returns (findings, has_fail). Findings with level FAIL should block ingest.
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
        # Without a valid header we cannot check rows meaningfully.
        return findings, True

    seen_sample_ids = set()
    metadata_r1r2 = set()
    for i, row in enumerate(rows, start=2):  # line 2 = first data row
        sample_id = (row.get("ID") or "").strip()
        r1 = (row.get("R1") or "").strip()
        r2 = (row.get("R2") or "").strip()
        taxon = (row.get("Taxon") or "").strip()
        uniq_id = (row.get(uniqid_col) or "").strip()

        missing = [c for c, v in (("ID", sample_id), ("R1", r1), ("R2", r2),
                                  ("Taxon", taxon), ("UniqID", uniq_id)) if not v]
        if missing:
            findings.append(Finding(FAIL, f"row {i}: empty required field(s): {', '.join(missing)}"))
            continue

        if r1 == r2:
            findings.append(Finding(FAIL, f"row {i}: R1 and R2 are identical ({r1})"))

        if sample_id in seen_sample_ids:
            findings.append(Finding(FAIL, f"row {i}: duplicate sample ID '{sample_id}'"))
        seen_sample_ids.add(sample_id)

        metadata_r1r2.update([r1, r2])

        if known_uniq_ids and uniq_id in known_uniq_ids:
            findings.append(Finding(
                WARN,
                f"row {i}: UniqID '{uniq_id}' already cataloged under project "
                f"'{known_uniq_ids[uniq_id]}' (possible re-sequence)"))

        if disk_filenames is not None:
            if r1 not in disk_filenames:
                findings.append(Finding(WARN, f"row {i}: R1 '{r1}' not found on disk"))
            if r2 not in disk_filenames:
                findings.append(Finding(WARN, f"row {i}: R2 '{r2}' not found on disk"))

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
    'unchecked' when the data dir is unreachable.
    """
    db_files = {r["filename"] for r in conn.execute(
        "SELECT filename FROM files WHERE project_id=?", (project_id,))}
    data_dir = os.path.join(seqdata_root, seq_data_relpath)
    if not os.path.isdir(data_dir):
        return {"status": "unchecked", "n_missing": None, "n_orphan": None,
                "missing": [], "orphan": []}
    disk = {os.path.basename(p)
            for p in glob.glob(os.path.join(data_dir, "*.fastq.gz"))}
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
        "SELECT project_id, seq_data_relpath FROM projects ORDER BY project_id"
    ).fetchall()

    shared = {}
    for r in conn.execute(
        """SELECT uniq_id, GROUP_CONCAT(DISTINCT project_id) AS projects,
                  COUNT(DISTINCT project_id) AS n
           FROM samples WHERE uniq_id IS NOT NULL AND uniq_id != ''
           GROUP BY uniq_id HAVING n > 1"""):
        for project_id in r["projects"].split(","):
            shared.setdefault(project_id, []).append((r["uniq_id"], r["projects"]))

    today = date.today().isoformat()
    results = {}
    for p in projects:
        project_id = p["project_id"]

        if seqdata_root:
            data = check_data_files(conn, project_id, p["seq_data_relpath"], seqdata_root)
            if data["status"] != "unchecked":
                conn.execute(
                    """UPDATE projects SET data_check_status=?, data_check_n_missing=?,
                         data_check_n_orphan=?, data_check_date=? WHERE project_id=?""",
                    (data["status"], data["n_missing"], data["n_orphan"], today, project_id))
                _persist_data_check_issues(conn, project_id, data["missing"], data["orphan"])
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

    conn.commit()
    return results

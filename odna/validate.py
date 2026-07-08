"""Validation of map file metadata against sequence data.

Rules are split into FAIL (reject ingest) and WARN (record, proceed). Matching
between metadata and files is done by EXACT basename, both directions, fixing the
substring bug in the old validate_seq_data.py.
"""

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


def validate_catalog(conn):
    """Re-check the whole catalog from stored data. Returns {project_id: [Finding]}.

    Reports: files with no recorded md5, Store/P-drive md5 mismatches, and UniqIDs
    shared across projects.
    """
    results = {}

    def add(project_id, level, message):
        results.setdefault(project_id, []).append(Finding(level, message))

    cur = conn.execute(
        "SELECT project_id, filename, md5, md5_match FROM files ORDER BY project_id, filename")
    for r in cur.fetchall():
        if r["md5"] is None:
            add(r["project_id"], WARN, f"{r['filename']}: no md5 recorded (not yet checksummed)")
        elif r["md5_match"] == 0:
            add(r["project_id"], WARN, f"{r['filename']}: Store/P-drive md5 mismatch")

    cur = conn.execute(
        """SELECT uniq_id, GROUP_CONCAT(DISTINCT project_id) AS projects, COUNT(DISTINCT project_id) AS n
           FROM samples WHERE uniq_id IS NOT NULL AND uniq_id != ''
           GROUP BY uniq_id HAVING n > 1""")
    for r in cur.fetchall():
        for project_id in r["projects"].split(","):
            add(project_id, WARN,
                f"UniqID '{r['uniq_id']}' shared across projects: {r['projects']}")

    return results

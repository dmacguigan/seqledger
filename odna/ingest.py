"""Ingest CSV map files into the catalog (metadata only; no hashing)."""

import csv
import glob
import json
import os
from datetime import date

from .db import header_uniqid_column, parse_project_id
from .validate import validate_metadata, overall_status


def read_map_file(map_file_path):
    """Read the two-column map file (metadata csv, data dir). Header row ignored.

    Returns list of (metadata_filename, seq_data_relpath).
    """
    entries = []
    with open(map_file_path) as f:
        next(f, None)  # skip header
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            entries.append((parts[0], parts[1]))
    return entries


def _read_csv(metadata_path):
    with open(metadata_path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        rows = []
        for raw in reader:
            if not any(cell.strip() for cell in raw):
                continue
            rows.append({header[i]: raw[i] for i in range(min(len(header), len(raw)))})
    return header, rows


def _known_uniq_ids(conn, exclude_project):
    cur = conn.execute(
        "SELECT uniq_id, project_id FROM samples WHERE uniq_id IS NOT NULL AND project_id != ?",
        (exclude_project,))
    return {r["uniq_id"]: r["project_id"] for r in cur.fetchall()}


def _upsert_project(conn, project_id, source, number, description, metadata_filename,
                    seq_data_relpath):
    conn.execute(
        """INSERT INTO projects
             (project_id, source, project_number, description, metadata_file,
              seq_data_relpath, date_ingested)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(project_id) DO UPDATE SET
             source=excluded.source, project_number=excluded.project_number,
             description=excluded.description, metadata_file=excluded.metadata_file,
             seq_data_relpath=excluded.seq_data_relpath,
             date_ingested=excluded.date_ingested""",
        (project_id, source, number, description, metadata_filename,
         seq_data_relpath, date.today().isoformat()))


def _upsert_sample(conn, project_id, sample_id, taxon, uniq_id, extra_json):
    conn.execute(
        """INSERT INTO samples (project_id, sample_id, taxon, uniq_id, extra_json)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(project_id, sample_id) DO UPDATE SET
             taxon=excluded.taxon, uniq_id=excluded.uniq_id, extra_json=excluded.extra_json""",
        (project_id, sample_id, taxon, uniq_id, extra_json))
    cur = conn.execute(
        "SELECT sample_pk FROM samples WHERE project_id=? AND sample_id=?",
        (project_id, sample_id))
    return cur.fetchone()["sample_pk"]


def _upsert_file(conn, project_id, sample_pk, role, filename, rel_path):
    # Do not clobber md5 columns on re-ingest.
    conn.execute(
        """INSERT INTO files (project_id, sample_pk, role, filename, rel_path)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(project_id, filename) DO UPDATE SET
             sample_pk=excluded.sample_pk, role=excluded.role, rel_path=excluded.rel_path""",
        (project_id, sample_pk, role, filename, rel_path))


def ingest_project(conn, metadata_path, seq_data_relpath, seqdata_root=None):
    """Validate and load one project. Returns (project_id, findings, status)."""
    metadata_filename = os.path.basename(metadata_path)
    project_id, source, number, description = parse_project_id(seq_data_relpath)

    header, rows = _read_csv(metadata_path)

    disk_filenames = None
    if seqdata_root:
        data_dir = os.path.join(seqdata_root, seq_data_relpath)
        disk_filenames = {os.path.basename(p)
                          for p in glob.glob(os.path.join(data_dir, "*.fastq.gz"))}

    findings, has_fail = validate_metadata(
        metadata_filename, header, rows, disk_filenames,
        known_uniq_ids=_known_uniq_ids(conn, project_id))
    status = overall_status(findings)

    if has_fail:
        return project_id, findings, status

    uniqid_col = header_uniqid_column(header)
    _upsert_project(conn, project_id, source, number, description,
                    metadata_filename, seq_data_relpath)

    core = set(["ID", "R1", "R2", "Taxon", uniqid_col])
    for row in rows:
        sample_id = row["ID"].strip()
        taxon = row["Taxon"].strip()
        uniq_id = row[uniqid_col].strip()
        extra = {k: v for k, v in row.items() if k not in core}
        extra_json = json.dumps(extra) if extra else None
        sample_pk = _upsert_sample(conn, project_id, sample_id, taxon, uniq_id, extra_json)

        for role in ("R1", "R2"):
            filename = row[role].strip()
            rel_path = os.path.join(seq_data_relpath, filename)
            _upsert_file(conn, project_id, sample_pk, role, filename, rel_path)

    conn.commit()
    return project_id, findings, status


def ingest_map_file(conn, map_file_path, seqdata_root=None):
    """Ingest all projects listed in a two-column map file."""
    results = []
    metadata_root = os.path.dirname(os.path.abspath(map_file_path))
    for metadata_filename, seq_data_relpath in read_map_file(map_file_path):
        metadata_path = metadata_filename
        if not os.path.isabs(metadata_path) and not os.path.exists(metadata_path):
            metadata_path = os.path.join(metadata_root, metadata_filename)
        results.append(ingest_project(conn, metadata_path, seq_data_relpath, seqdata_root))
    return results

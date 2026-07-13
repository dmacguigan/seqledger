"""Ingest CSV map files into the catalog (metadata only; no hashing)."""

import csv
import glob
import io
import json
import os
from datetime import date

try:
    import pwd
except ImportError:  # non-Unix; ownership capture unavailable
    pwd = None

from .db import header_uniqid_column, parse_project_id
from .validate import validate_metadata, overall_status


def _owner_name(uid, cache):
    """Resolve a uid to a username, cached. Falls back to the numeric uid."""
    if uid in cache:
        return cache[uid]
    name = str(uid)
    if pwd is not None:
        try:
            name = pwd.getpwuid(uid).pw_name
        except KeyError:
            pass
    cache[uid] = name
    return name


def _stat_owner(path, cache):
    """Return (owner_uid, owner_name, size_bytes) for a path, or (None, None, None)."""
    try:
        st = os.stat(path)
    except OSError:
        return None, None, None
    return st.st_uid, _owner_name(st.st_uid, cache), st.st_size


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


def _read_text(path):
    """Read a metadata CSV, tolerating Excel exports that aren't UTF-8.

    Tries UTF-8 (with BOM), then falls back to cp1252 (Windows Excel default).
    """
    with open(path, "rb") as f:
        data = f.read()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_csv(metadata_path):
    reader = csv.reader(io.StringIO(_read_text(metadata_path)))
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
                    seq_data_relpath, seqdata_root=None, owner_uid=None, owner_name=None):
    conn.execute(
        """INSERT INTO projects
             (project_id, source, project_number, description, metadata_file,
              seq_data_relpath, seqdata_root, owner_uid, owner_name, date_ingested)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(project_id) DO UPDATE SET
             source=excluded.source, project_number=excluded.project_number,
             description=excluded.description, metadata_file=excluded.metadata_file,
             seq_data_relpath=excluded.seq_data_relpath,
             seqdata_root=excluded.seqdata_root, owner_uid=excluded.owner_uid,
             owner_name=excluded.owner_name, date_ingested=excluded.date_ingested""",
        (project_id, source, number, description, metadata_filename,
         seq_data_relpath, seqdata_root, owner_uid, owner_name, date.today().isoformat()))


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


def _upsert_file(conn, project_id, sample_pk, read, filename, rel_path,
                 size_bytes=None, owner_uid=None, owner_name=None):
    # Do not clobber md5 columns on re-ingest. Refresh size/owner only when known
    # (COALESCE keeps prior values if the file was unreachable this run).
    was_new = conn.execute(
        "SELECT 1 FROM files WHERE project_id=? AND filename=?",
        (project_id, filename)).fetchone() is None
    conn.execute(
        """INSERT INTO files
             (project_id, sample_pk, read, filename, rel_path,
              size_bytes, owner_uid, owner_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(project_id, filename) DO UPDATE SET
             sample_pk=excluded.sample_pk, read=excluded.read, rel_path=excluded.rel_path,
             size_bytes=COALESCE(excluded.size_bytes, files.size_bytes),
             owner_uid=COALESCE(excluded.owner_uid, files.owner_uid),
             owner_name=COALESCE(excluded.owner_name, files.owner_name)""",
        (project_id, sample_pk, read, filename, rel_path,
         size_bytes, owner_uid, owner_name))
    return was_new


def ingest_project(conn, metadata_path, seq_data_relpath, seqdata_root=None):
    """Validate and load one project.

    Returns (project_id, findings, status, stats). `stats` summarizes what the
    upsert changed so the caller can drive integrity/taxonomy only where needed:
      new_samples    -- sample_ids not previously in the catalog
      changed_samples-- existing samples whose Taxon changed in the CSV
      new_files      -- file rows created (candidates for an integrity check)
      changed_taxa   -- set of new/changed Taxon strings (may need re-resolving)
      orphan_samples -- sample_ids in the catalog but absent from this CSV
    On a FAIL (nothing written), stats is all-zero/empty.
    """
    metadata_filename = os.path.basename(metadata_path)
    project_id, source, number, description = parse_project_id(seq_data_relpath)

    header, rows = _read_csv(metadata_path)

    disk_filenames = None
    proj_owner_uid = proj_owner_name = None
    disk_stats = {}  # filename -> (size_bytes, owner_uid, owner_name)
    uid_cache = {}
    if seqdata_root:
        data_dir = os.path.join(seqdata_root, seq_data_relpath)
        proj_owner_uid, proj_owner_name, _ = _stat_owner(data_dir, uid_cache)
        disk_filenames = set()
        for p in glob.glob(os.path.join(data_dir, "*.fastq.gz")):
            fn = os.path.basename(p)
            disk_filenames.add(fn)
            uid, name, size = _stat_owner(p, uid_cache)
            disk_stats[fn] = (size, uid, name)

    findings, has_fail = validate_metadata(
        metadata_filename, header, rows, disk_filenames,
        known_uniq_ids=_known_uniq_ids(conn, project_id))
    status = overall_status(findings)

    stats = {"new_samples": 0, "changed_samples": 0, "new_files": 0,
             "changed_taxa": set(), "orphan_samples": []}
    if has_fail:
        return project_id, findings, status, stats

    uniqid_col = header_uniqid_column(header)
    abs_root = os.path.abspath(seqdata_root) if seqdata_root else None
    _upsert_project(conn, project_id, source, number, description,
                    metadata_filename, seq_data_relpath, seqdata_root=abs_root,
                    owner_uid=proj_owner_uid, owner_name=proj_owner_name)

    # Snapshot existing samples so we can tell new vs changed vs (dropped) orphans.
    existing = {r["sample_id"]: r["taxon"] for r in conn.execute(
        "SELECT sample_id, taxon FROM samples WHERE project_id=?", (project_id,))}
    csv_ids = set()

    core = set(["ID", "R1", "R2", "Taxon", uniqid_col])
    for row in rows:
        sample_id = row["ID"].strip()
        taxon = row["Taxon"].strip()
        uniq_id = row[uniqid_col].strip()
        csv_ids.add(sample_id)
        if sample_id not in existing:
            stats["new_samples"] += 1
            if taxon:
                stats["changed_taxa"].add(taxon)
        elif existing[sample_id] != taxon:
            stats["changed_samples"] += 1
            if taxon:
                stats["changed_taxa"].add(taxon)
        extra = {k: v for k, v in row.items() if k not in core}
        extra_json = json.dumps(extra) if extra else None
        sample_pk = _upsert_sample(conn, project_id, sample_id, taxon, uniq_id, extra_json)

        for read in ("R1", "R2"):
            filename = row[read].strip()
            rel_path = os.path.join(seq_data_relpath, filename)
            size, uid, name = disk_stats.get(filename, (None, None, None))
            if _upsert_file(conn, project_id, sample_pk, read, filename, rel_path,
                            size_bytes=size, owner_uid=uid, owner_name=name):
                stats["new_files"] += 1

    stats["orphan_samples"] = sorted(set(existing) - csv_ids)
    conn.commit()
    return project_id, findings, status, stats


def ingest_map_file(conn, map_file_path, seqdata_root=None, metadata_root=None):
    """Ingest all projects listed in a two-column map file.

    Per-project metadata CSVs are resolved relative to metadata_root when their
    path is not absolute and not found in the CWD. Defaults to the map file's
    own directory.
    """
    results = []
    if metadata_root is None:
        metadata_root = os.path.dirname(os.path.abspath(map_file_path))
    for metadata_filename, seq_data_relpath in read_map_file(map_file_path):
        metadata_path = metadata_filename
        if not os.path.isabs(metadata_path) and not os.path.exists(metadata_path):
            metadata_path = os.path.join(metadata_root, metadata_filename)
        results.append(ingest_project(conn, metadata_path, seq_data_relpath, seqdata_root))
    return results

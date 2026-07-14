"""Ingest CSV map files into the catalog (metadata only; no hashing).

Two ways in:
  ingest_tree      auto-discover: point at a seqdata root + a metadata root and
                   pair each project folder with its `<project>_mapfile.csv`.
  ingest_map_file  explicit two-column map file (metadata csv, data dir). Kept for
                   manual/odd layouts and back-compat.

Auto-discovery records a per-project `metadata_status` for pairing problems:
  ok               mapfile present + parseable, folder present
  missing_mapfile  project folder on disk, no mapfile -> files cataloged, no samples
  missing_seqdata  mapfile present, no project folder -> samples cataloged, no files
  broken_mapfile   mapfile present but header is malformed -> files cataloged, no samples
In every case the project is still added to the catalog (so nothing is hidden).
"""

import csv
import glob
import io
import json
import os
import re
from datetime import date

try:
    import pwd
except ImportError:  # non-Unix; ownership capture unavailable
    pwd = None

from .db import METADATA_SUFFIX, REQUIRED_COLUMNS, header_uniqid_column, parse_project_id
from .validate import Finding, FAIL, WARN, validate_metadata, overall_status

# Plain-english explanations stored on the project row (also surfaced in the GUI).
MISSING_MAPFILE_DETAIL = (
    "No matching '<project>_mapfile.csv' was found in the metadata directory for "
    "this sequence-data folder. The FASTQ files were cataloged from disk, but "
    "sample metadata (taxon, UniqID) is missing until a mapfile is added.")
MISSING_SEQDATA_DETAIL = (
    "A mapfile exists but no matching folder was found in the sequence-data "
    "directory. Samples were cataloged from the mapfile, but no files are on disk.")
_BROKEN_MAPFILE_DETAIL = (
    "The mapfile is present but could not be parsed: the first columns must be "
    "{required},UniqID (or UniqueID). The FASTQ files were cataloged from disk; "
    "sample metadata was skipped until the mapfile is fixed.")


def _broken_mapfile_detail(header):
    got = ",".join(h.strip() for h in header[:5]) if header else "(empty/unreadable)"
    return (_BROKEN_MAPFILE_DETAIL.format(required=",".join(REQUIRED_COLUMNS))
            + f" Got: {got}.")


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


_DIRECTION_RE = re.compile(r"(?:^|[._-])(?:R?([12]))\.fastq\.gz$", re.IGNORECASE)


def _infer_direction(filename):
    """Best-effort R1/R2 from a FASTQ filename (e.g. x_1.fastq.gz, x_R2.fastq.gz)."""
    m = _DIRECTION_RE.search(filename)
    return f"R{m.group(1)}" if m else None


def _discover_disk_files(data_dir, seqdata_root, uid_cache):
    """Find *.fastq.gz under data_dir (recursively; files may be nested in subdirs).

    Returns ({basename: {"rel_path", "size", "uid", "name"}}, collisions), where
    rel_path is relative to seqdata_root and collisions lists basenames that appear
    in more than one subdirectory (last one wins).
    """
    found = {}
    collisions = []
    for p in sorted(glob.glob(os.path.join(data_dir, "**", "*.fastq.gz"), recursive=True)):
        fn = os.path.basename(p)
        if fn in found:
            collisions.append(fn)
        uid, name, size = _stat_owner(p, uid_cache)
        found[fn] = {"rel_path": os.path.relpath(p, seqdata_root),
                     "size": size, "uid": uid, "name": name}
    return found, collisions


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


def _mapfile_header_ok(mapfile_path):
    """(header_is_valid, header_list) for a mapfile, tolerating read errors."""
    try:
        header, _ = _read_csv(mapfile_path)
    except OSError:
        return False, []
    return header_uniqid_column(header) is not None, header


def _known_uniq_ids(conn, exclude_project):
    cur = conn.execute(
        "SELECT uniq_id, project_id FROM samples WHERE uniq_id IS NOT NULL AND project_id != ?",
        (exclude_project,))
    return {r["uniq_id"]: r["project_id"] for r in cur.fetchall()}


def _upsert_project(conn, project_id, source, number, description, metadata_filename,
                    seq_data_relpath, seqdata_root=None, owner_uid=None, owner_name=None,
                    metadata_status="ok", metadata_detail=None):
    conn.execute(
        """INSERT INTO projects
             (project_id, source, project_number, description, metadata_file,
              seq_data_relpath, seqdata_root, owner_uid, owner_name, date_ingested,
              metadata_status, metadata_detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(project_id) DO UPDATE SET
             source=excluded.source, project_number=excluded.project_number,
             description=excluded.description, metadata_file=excluded.metadata_file,
             seq_data_relpath=excluded.seq_data_relpath,
             seqdata_root=excluded.seqdata_root, owner_uid=excluded.owner_uid,
             owner_name=excluded.owner_name, date_ingested=excluded.date_ingested,
             metadata_status=excluded.metadata_status,
             metadata_detail=excluded.metadata_detail""",
        (project_id, source, number, description, metadata_filename,
         seq_data_relpath, seqdata_root, owner_uid, owner_name, date.today().isoformat(),
         metadata_status, metadata_detail))


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


def _upsert_file(conn, project_id, sample_pk, direction, filename, rel_path,
                 size_bytes=None, owner_uid=None, owner_name=None):
    # Do not clobber md5 columns on re-ingest. Refresh size/owner only when known
    # (COALESCE keeps prior values if the file was unreachable this run).
    was_new = conn.execute(
        "SELECT 1 FROM files WHERE project_id=? AND filename=?",
        (project_id, filename)).fetchone() is None
    conn.execute(
        """INSERT INTO files
             (project_id, sample_pk, direction, filename, rel_path,
              size_bytes, owner_uid, owner_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(project_id, filename) DO UPDATE SET
             sample_pk=excluded.sample_pk, direction=excluded.direction, rel_path=excluded.rel_path,
             size_bytes=COALESCE(excluded.size_bytes, files.size_bytes),
             owner_uid=COALESCE(excluded.owner_uid, files.owner_uid),
             owner_name=COALESCE(excluded.owner_name, files.owner_name)""",
        (project_id, sample_pk, direction, filename, rel_path,
         size_bytes, owner_uid, owner_name))
    return was_new


def _empty_stats():
    return {"new_samples": 0, "changed_samples": 0, "new_files": 0,
            "changed_taxa": set(), "orphan_samples": [], "pruned_samples": [],
            "pruned_files": 0, "metadata_status": "ok", "metadata_detail": None}


def ingest_project(conn, metadata_path, seq_data_relpath, seqdata_root=None, prune=False,
                   metadata_status="ok", metadata_detail=None):
    """Validate and load one project from its mapfile.

    Returns (project_id, findings, status, stats). `stats` summarizes what the
    upsert changed so the caller can drive integrity/taxonomy only where needed:
      new_samples    -- sample_ids not previously in the catalog
      changed_samples-- existing samples whose Taxon changed in the CSV
      new_files      -- file rows created (candidates for an integrity check)
      changed_taxa   -- set of new/changed Taxon strings (may need re-resolving)
      orphan_samples -- sample_ids in the catalog but absent from this CSV
      pruned_samples -- sample_ids deleted from the catalog (only when prune=True)
      pruned_files   -- count of stale file rows deleted (only when prune=True)
      metadata_status/metadata_detail -- mapfile<->folder pairing health (see module doc)

    FASTQ files are discovered recursively under the data dir, so files nested in
    subdirectories are found and their real (nested) rel_path is recorded.

    With prune=True, rows that the (corrected) CSV no longer references are
    deleted: samples dropped from the CSV (their files cascade) and any file row
    whose filename is no longer listed (e.g. a filename typo fixed in place).
    On a FAIL (nothing written), stats is all-zero/empty and no pruning happens.
    """
    metadata_filename = os.path.basename(metadata_path)
    project_id, source, number, description = parse_project_id(seq_data_relpath)

    header, rows = _read_csv(metadata_path)

    disk = None  # {basename: {rel_path,size,uid,name}}
    proj_owner_uid = proj_owner_name = None
    collisions = []
    uid_cache = {}
    if seqdata_root:
        data_dir = os.path.join(seqdata_root, seq_data_relpath)
        proj_owner_uid, proj_owner_name, _ = _stat_owner(data_dir, uid_cache)
        disk, collisions = _discover_disk_files(data_dir, seqdata_root, uid_cache)

    disk_filenames = set(disk) if disk is not None else None
    findings, has_fail = validate_metadata(
        metadata_filename, header, rows, disk_filenames,
        known_uniq_ids=_known_uniq_ids(conn, project_id))
    for fn in collisions:
        findings.append(Finding(WARN, f"'{fn}' appears in more than one subdirectory; "
                                       "the last one found was cataloged"))
    status = overall_status(findings)

    stats = _empty_stats()
    stats["metadata_status"] = metadata_status
    stats["metadata_detail"] = metadata_detail
    if has_fail:
        return project_id, findings, status, stats

    uniqid_col = header_uniqid_column(header)
    abs_root = os.path.abspath(seqdata_root) if seqdata_root else None
    _upsert_project(conn, project_id, source, number, description,
                    metadata_filename, seq_data_relpath, seqdata_root=abs_root,
                    owner_uid=proj_owner_uid, owner_name=proj_owner_name,
                    metadata_status=metadata_status, metadata_detail=metadata_detail)

    # Snapshot existing samples so we can tell new vs changed vs (dropped) orphans.
    existing = {r["sample_id"]: r["taxon"] for r in conn.execute(
        "SELECT sample_id, taxon FROM samples WHERE project_id=?", (project_id,))}
    csv_ids = set()
    csv_files = set()

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

        for direction in ("R1", "R2"):
            filename = row[direction].strip()
            csv_files.add(filename)
            info = disk.get(filename) if disk else None
            rel_path = info["rel_path"] if info else os.path.join(seq_data_relpath, filename)
            size = info["size"] if info else None
            uid = info["uid"] if info else None
            name = info["name"] if info else None
            if _upsert_file(conn, project_id, sample_pk, direction, filename, rel_path,
                            size_bytes=size, owner_uid=uid, owner_name=name):
                stats["new_files"] += 1

    stats["orphan_samples"] = sorted(set(existing) - csv_ids)

    if prune:
        # Drop samples the corrected CSV no longer lists (files cascade), then any
        # remaining file row whose filename is no longer referenced (in-place fix).
        conn.executemany(
            "DELETE FROM samples WHERE project_id=? AND sample_id=?",
            [(project_id, sid) for sid in stats["orphan_samples"]])
        stats["pruned_samples"] = list(stats["orphan_samples"])
        placeholders = ",".join("?" * len(csv_files)) or "NULL"
        cur = conn.execute(
            f"DELETE FROM files WHERE project_id=? AND filename NOT IN ({placeholders})",
            (project_id, *csv_files))
        stats["pruned_files"] = cur.rowcount

    conn.commit()
    return project_id, findings, status, stats


def _ingest_diskonly(conn, project_id, data_dir, seqdata_root, metadata_status, detail):
    """Catalog a project's on-disk FASTQ files with no sample metadata.

    Used when a project has no usable mapfile (missing_mapfile / broken_mapfile):
    the project row is created (flagged with metadata_status) and every *.fastq.gz
    found on disk is cataloged with sample_pk NULL and a best-effort R1/R2 guess, so
    the files are visible and can still be integrity-checked. Returns a stats dict.
    """
    uid_cache = {}
    project_id_, source, number, description = parse_project_id(project_id)
    abs_root = os.path.abspath(seqdata_root) if seqdata_root else None
    proj_uid = proj_name = None
    disk = {}
    if data_dir:
        proj_uid, proj_name, _ = _stat_owner(data_dir, uid_cache)
        disk, _coll = _discover_disk_files(data_dir, seqdata_root, uid_cache)

    _upsert_project(conn, project_id_, source, number, description, None, project_id,
                    seqdata_root=abs_root, owner_uid=proj_uid, owner_name=proj_name,
                    metadata_status=metadata_status, metadata_detail=detail)

    stats = _empty_stats()
    stats["metadata_status"] = metadata_status
    stats["metadata_detail"] = detail
    for fn, info in sorted(disk.items()):
        if _upsert_file(conn, project_id_, None, _infer_direction(fn), fn,
                        info["rel_path"], size_bytes=info["size"],
                        owner_uid=info["uid"], owner_name=info["name"]):
            stats["new_files"] += 1
    conn.commit()
    return stats


def discover_projects(seqdata_root, metadata_root):
    """Pair each project folder under seqdata_root with its metadata mapfile.

    A project is any top-level directory in seqdata_root; its mapfile is
    '<project>_mapfile.csv' in metadata_root. The union of both sides is returned
    (so folders with no mapfile and mapfiles with no folder both surface).
    Returns a list of {project_id, data_dir (abs or None), mapfile (path or None)}.
    """
    disk = {}
    if seqdata_root and os.path.isdir(seqdata_root):
        for name in sorted(os.listdir(seqdata_root)):
            full = os.path.join(seqdata_root, name)
            if os.path.isdir(full):
                disk[name] = full
    mapfiles = {}
    if metadata_root and os.path.isdir(metadata_root):
        for p in sorted(glob.glob(os.path.join(metadata_root, "*" + METADATA_SUFFIX))):
            pid = os.path.basename(p)[:-len(METADATA_SUFFIX)]
            mapfiles[pid] = p
    return [{"project_id": pid, "data_dir": disk.get(pid), "mapfile": mapfiles.get(pid)}
            for pid in sorted(set(disk) | set(mapfiles))]


def ingest_tree(conn, seqdata_root, metadata_root, prune=False):
    """Auto-discover and ingest every project under seqdata_root / metadata_root.

    Each project folder is paired with its '<project>_mapfile.csv'. Pairing problems
    are flagged on the project row (metadata_status) but never block ingest:
      - folder + valid mapfile      -> samples + files (metadata_status 'ok')
      - mapfile only (no folder)    -> samples from mapfile (metadata_status 'missing_seqdata')
      - folder only (no mapfile)    -> files from disk, no samples ('missing_mapfile')
      - folder + broken mapfile     -> files from disk, no samples ('broken_mapfile')
    Returns the same list of (project_id, findings, status, stats) as ingest_map_file.
    """
    results = []
    for proj in discover_projects(seqdata_root, metadata_root):
        pid = proj["project_id"]
        data_dir = proj["data_dir"]
        mapfile = proj["mapfile"]
        has_dir = data_dir is not None

        if mapfile is None:  # folder on disk, no mapfile
            stats = _ingest_diskonly(conn, pid, data_dir, seqdata_root,
                                     "missing_mapfile", MISSING_MAPFILE_DETAIL)
            results.append((pid, [Finding(WARN, "no mapfile for this project folder; "
                                                "files cataloged without sample metadata")],
                            "warn", stats))
            continue

        header_ok, header = _mapfile_header_ok(mapfile)
        if not header_ok:  # mapfile present but unparseable
            detail = _broken_mapfile_detail(header)
            stats = _ingest_diskonly(conn, pid, data_dir, seqdata_root,
                                     "broken_mapfile", detail)
            results.append((pid, [Finding(FAIL, "mapfile header is malformed; "
                                                "sample metadata skipped")],
                            "fail", stats))
            continue

        # Parseable mapfile: normal load. Flag missing_seqdata when no folder exists.
        mstatus = "ok" if has_dir else "missing_seqdata"
        detail = None if has_dir else MISSING_SEQDATA_DETAIL
        pid_, findings, status, stats = ingest_project(
            conn, mapfile, pid, seqdata_root=seqdata_root, prune=prune,
            metadata_status=mstatus, metadata_detail=detail)

        if status == "fail":
            # Valid mapfile structure but content failed validation (e.g. duplicate
            # IDs). Still record the project + its on-disk files so nothing is hidden;
            # the failure stays in findings and is logged by the caller.
            stats = _ingest_diskonly(conn, pid, data_dir, seqdata_root, mstatus, detail)
            findings = findings  # keep the FAIL findings
        elif not has_dir:
            findings = [Finding(WARN, "no project folder on disk for this mapfile")] + findings
            if status == "pass":
                status = "warn"
        stats["metadata_status"] = mstatus
        stats["metadata_detail"] = detail
        results.append((pid_, findings, status, stats))
    return results


def prune_missing_projects(conn, seqdata_root, metadata_root):
    """Delete catalog projects that vanished from BOTH roots (folder + mapfile gone).

    Only for auto-discovery. Cascades to the project's samples/files/backups/etc.
    Safety: if discovery turns up nothing (roots empty / unmounted / unreadable),
    this refuses to prune and returns skipped=True, so a missing mount can't wipe
    the catalog. Only projects whose stored seqdata_root matches this run's root are
    eligible, so projects ingested from a different root are never touched.
    Returns {"pruned": [project_id, ...], "skipped": bool}.
    """
    discovered = {p["project_id"] for p in discover_projects(seqdata_root, metadata_root)}
    if not discovered:
        return {"pruned": [], "skipped": True}
    abs_root = os.path.abspath(seqdata_root)
    in_db = [r["project_id"] for r in conn.execute(
        "SELECT project_id FROM projects WHERE seqdata_root=?", (abs_root,))]
    gone = sorted(pid for pid in in_db if pid not in discovered)
    conn.executemany("DELETE FROM projects WHERE project_id=?", [(p,) for p in gone])
    conn.commit()
    return {"pruned": gone, "skipped": False}


def ingest_map_file(conn, map_file_path, seqdata_root=None, metadata_root=None, prune=False):
    """Ingest all projects listed in a two-column map file.

    Per-project metadata CSVs are resolved relative to metadata_root when their
    path is not absolute and not found in the CWD. Defaults to the map file's
    own directory. prune=True is forwarded to each project (see ingest_project).
    """
    results = []
    if metadata_root is None:
        metadata_root = os.path.dirname(os.path.abspath(map_file_path))
    for metadata_filename, seq_data_relpath in read_map_file(map_file_path):
        metadata_path = metadata_filename
        if not os.path.isabs(metadata_path) and not os.path.exists(metadata_path):
            metadata_path = os.path.join(metadata_root, metadata_filename)
        results.append(ingest_project(conn, metadata_path, seq_data_relpath,
                                      seqdata_root, prune=prune))
    return results

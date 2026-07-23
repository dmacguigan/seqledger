"""Ingest CSV map files into the catalog (metadata only; no hashing).

Two ways in:
  ingest_tree      auto-discover: point at a seqdata root + a metadata root and
                   pair each project folder with its `<project>_mapfile.csv`.
  ingest_map_file  explicit two-column map file (metadata csv, data dir). Kept for
                   manual/odd layouts and back-compat.

Auto-discovery records a per-project `metadata_status`:
  ok               mapfile present + parseable, folder present, all rows clean
  flagged          header valid but some rows needed repair: empty Taxon/UniqID were
                   filled with 'NA', empty-ID / duplicate-ID rows were skipped. The
                   loadable samples ARE loaded; samples.flags records what was repaired.
  missing_mapfile  project folder on disk, no mapfile -> files cataloged, no samples
  missing_seqdata  mapfile present, no project folder -> samples cataloged, no files
  broken_mapfile   mapfile header is malformed -> files cataloged, no samples
  invalid_mapfile  header valid but NO rows were loadable -> files cataloged, no samples
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

from .db import (METADATA_SUFFIX, REQUIRED_COLUMNS, fastq_globs, get_config,
                 header_uniqid_column, parse_project_id)

# Fallback FASTQ globs when no config is available (direct callers / tests).
_DEFAULT_FASTQ_GLOBS = ("*.fastq.gz", "*.fq.gz")
# Chunk size for batched DELETEs so a big project can't exceed SQLITE_MAX_VARIABLE_NUMBER.
_DELETE_CHUNK = 500
from .validate import (Finding, FAIL, WARN, NA_VALUE, FLAG_MESSAGES,
                       plan_rows, validate_metadata, overall_status)

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


_INVALID_MAPFILE_PREFIX = (
    "The mapfile header is valid but some rows failed validation, so no samples were "
    "loaded (the FASTQ files were still cataloged from disk). Fix the flagged rows in "
    "the mapfile -- e.g. add UniqIDs for control rows, or remove them -- and re-ingest. "
    "Problems: ")


def _invalid_mapfile_detail(findings, max_items=5):
    """One-line summary of the findings for a mapfile that loaded no samples."""
    msgs = [f.message for f in findings if f.level in (FAIL, WARN)]
    shown = "; ".join(msgs[:max_items])
    if len(msgs) > max_items:
        shown += f"; (+{len(msgs) - max_items} more)"
    return _INVALID_MAPFILE_PREFIX + (shown or "no usable rows") + "."


def _row_quality_status(passed_status, passed_detail, n_flagged, skipped):
    """Final metadata_status/detail for a project that loaded >=1 sample.

    'missing_seqdata' (no data folder) is the headline and wins. Otherwise, if any
    row was NA-filled or skipped, the project is 'flagged' with a count; else it
    keeps the status it came in with ('ok').
    """
    if passed_status == "missing_seqdata":
        return "missing_seqdata", passed_detail
    if n_flagged or skipped:
        parts = []
        if n_flagged:
            parts.append(f"{n_flagged} sample(s) had empty fields filled with NA")
        if skipped:
            ex = "; ".join(f"row {p['line']}: {p['skip_reason']}" for p in skipped[:3])
            more = f" (+{len(skipped) - 3} more)" if len(skipped) > 3 else ""
            parts.append(f"{len(skipped)} row(s) skipped -- {ex}{more}")
        detail = ("Mapfile loaded with issues: " + "; ".join(parts)
                  + ". Fix the mapfile and re-ingest to clear the flags.")
        return "flagged", detail
    return passed_status, passed_detail


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


# R1/R2 from a FASTQ name: the read token (optionally 'R') followed by an optional
# lane/chunk suffix (_001) and a .fastq.gz or .fq.gz extension. Matches bare
# 'x_1.fastq.gz', 'x_R2.fq.gz', and canonical bcl2fastq 'S1_L001_R1_001.fastq.gz'.
_DIRECTION_RE = re.compile(
    r"(?:^|[._-])R?([12])(?:_\d+)?\.(?:fastq|fq)\.gz$", re.IGNORECASE)


def _infer_direction(filename):
    """Best-effort R1/R2 from a FASTQ filename (e.g. x_1.fastq.gz, S1_L001_R2_001.fq.gz)."""
    m = _DIRECTION_RE.search(filename)
    return f"R{m.group(1)}" if m else None


def _discover_disk_files(data_dir, seqdata_root, uid_cache, globs=_DEFAULT_FASTQ_GLOBS):
    """Find FASTQ files under data_dir (recursively; files may be nested in subdirs).

    globs is the set of shell patterns to match (from the catalog's configured
    fastq_extensions, e.g. *.fastq.gz + *.fq.gz). Returns (by_basename, all_files,
    collisions), where each info is {"filename","rel_path","size","uid","name"} and
    rel_path is relative to seqdata_root:
      by_basename -- {basename: info} for mapfile R1/R2 lookup (a colliding basename
                     keeps the last found and is listed in `collisions`).
      all_files   -- one info per file (keyed physically by rel_path), so a caller
                     that catalogs everything (diskonly projects) keeps BOTH files
                     when two share a basename in different subdirs.
      collisions  -- basenames found in more than one subdir.
    """
    paths = set()
    for pat in globs:
        paths.update(glob.glob(os.path.join(data_dir, "**", pat), recursive=True))
    by_basename = {}
    all_files = []
    collisions = []
    for p in sorted(paths):
        fn = os.path.basename(p)
        uid, name, size = _stat_owner(p, uid_cache)
        info = {"filename": fn, "rel_path": os.path.relpath(p, seqdata_root),
                "size": size, "uid": uid, "name": name}
        if fn in by_basename:
            collisions.append(fn)
        by_basename[fn] = info
        all_files.append(info)
    return by_basename, all_files, collisions


def _cfg_fastq_globs(conn):
    """FASTQ globs for this catalog's configured extensions (default fastq.gz+fq.gz)."""
    return fastq_globs(get_config(conn, "fastq_extensions")) or list(_DEFAULT_FASTQ_GLOBS)


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


def _upsert_sample(conn, project_id, sample_id, taxon, uniq_id, extra_json, flags=None):
    conn.execute(
        """INSERT INTO samples (project_id, sample_id, taxon, uniq_id, extra_json, flags)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(project_id, sample_id) DO UPDATE SET
             taxon=excluded.taxon, uniq_id=excluded.uniq_id,
             extra_json=excluded.extra_json, flags=excluded.flags""",
        (project_id, sample_id, taxon, uniq_id, extra_json, flags))
    cur = conn.execute(
        "SELECT sample_pk FROM samples WHERE project_id=? AND sample_id=?",
        (project_id, sample_id))
    return cur.fetchone()["sample_pk"]


def _upsert_file(conn, project_id, sample_pk, direction, filename, rel_path,
                 size_bytes=None, owner_uid=None, owner_name=None, disk_confirmed=False,
                 proj_relpath=None):
    # A file's identity is its relative path (project_id, rel_path), not its basename:
    # two files can share a basename in different subdirs. Resolve the target rel_path
    # so a re-ingest updates the same row instead of creating a duplicate:
    #  - disk_confirmed (path came from a real disk scan): authoritative. If a single
    #    existing row for this basename still sits at the FLAT GUESS (proj_relpath/
    #    filename) recorded by a prior metadata-only run, move it onto the real disk
    #    path. A row already at a real, different path is a distinct physical file that
    #    merely shares a basename -- leave it, so both files are kept.
    #  - not disk_confirmed (rel_path is only a flat guess): reuse the existing row's
    #    recorded path when there's exactly one, so the guess never forks a new row.
    target_rel = rel_path
    rows = conn.execute(
        "SELECT file_pk, rel_path FROM files WHERE project_id=? AND filename=?",
        (project_id, filename)).fetchall()
    if disk_confirmed:
        flat_guess = os.path.join(proj_relpath, filename) if proj_relpath else None
        if (len(rows) == 1 and flat_guess is not None
                and rows[0]["rel_path"] == flat_guess and rel_path != flat_guess):
            taken = conn.execute(
                "SELECT 1 FROM files WHERE project_id=? AND rel_path=?",
                (project_id, rel_path)).fetchone()
            if not taken:
                conn.execute("UPDATE files SET rel_path=? WHERE file_pk=?",
                             (rel_path, rows[0]["file_pk"]))
    elif len(rows) == 1 and rows[0]["rel_path"]:
        target_rel = rows[0]["rel_path"]
    was_new = conn.execute(
        "SELECT 1 FROM files WHERE project_id=? AND rel_path=?",
        (project_id, target_rel)).fetchone() is None
    # Do not clobber md5 columns on re-ingest; refresh size/owner only when known
    # (COALESCE keeps prior values if the file was unreachable this run).
    conn.execute(
        """INSERT INTO files
             (project_id, sample_pk, direction, filename, rel_path,
              size_bytes, owner_uid, owner_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(project_id, rel_path) DO UPDATE SET
             sample_pk=excluded.sample_pk, direction=excluded.direction,
             filename=excluded.filename,
             size_bytes=COALESCE(excluded.size_bytes, files.size_bytes),
             owner_uid=COALESCE(excluded.owner_uid, files.owner_uid),
             owner_name=COALESCE(excluded.owner_name, files.owner_name)""",
        (project_id, sample_pk, direction, filename, target_rel,
         size_bytes, owner_uid, owner_name))
    return was_new


def _empty_stats():
    return {"new_samples": 0, "changed_samples": 0, "new_files": 0,
            "changed_taxa": set(), "orphan_samples": [], "pruned_samples": [],
            "pruned_files": 0, "metadata_status": "ok", "metadata_detail": None,
            "n_flagged": 0, "n_skipped": 0}


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
        disk, _all_disk, collisions = _discover_disk_files(
            data_dir, seqdata_root, uid_cache, _cfg_fastq_globs(conn))

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

    uniqid_col, plans = plan_rows(header, rows)
    usable = [p for p in plans if p["load"]]
    skipped = [p for p in plans if not p["load"]]

    # Nothing loadable (every row skipped / no rows): don't write a project row here.
    # ingest_tree falls back to _ingest_diskonly (invalid_mapfile); the manual path
    # just reports the failure. Preserves the "FAIL writes nothing" contract.
    if not usable:
        stats["metadata_status"] = "invalid_mapfile"
        stats["metadata_detail"] = _invalid_mapfile_detail(findings)
        return project_id, findings, "fail", stats

    n_flagged = sum(1 for p in usable if p["flags"])
    final_status, final_detail = _row_quality_status(
        metadata_status, metadata_detail, n_flagged, skipped)
    stats["metadata_status"] = final_status
    stats["metadata_detail"] = final_detail
    stats["n_flagged"] = n_flagged
    stats["n_skipped"] = len(skipped)

    abs_root = os.path.abspath(seqdata_root) if seqdata_root else None
    _upsert_project(conn, project_id, source, number, description,
                    metadata_filename, seq_data_relpath, seqdata_root=abs_root,
                    owner_uid=proj_owner_uid, owner_name=proj_owner_name,
                    metadata_status=final_status, metadata_detail=final_detail)

    # Snapshot existing samples so we can tell new vs changed vs (dropped) orphans.
    existing = {r["sample_id"]: r["taxon"] for r in conn.execute(
        "SELECT sample_id, taxon FROM samples WHERE project_id=?", (project_id,))}
    csv_ids = set()
    csv_files = set()

    # Exclude the core columns by their verbatim header cells (header validated
    # positionally, case-insensitively), so extra_json doesn't re-capture ID/R1/R2/
    # Taxon when the mapfile header uses non-canonical casing.
    core = {header[0], header[1], header[2], header[3], uniqid_col}
    seen_files = {}  # filename -> "sample_id/direction" it was first claimed by
    for p in usable:
        sample_id = p["sample_id"]
        taxon = p["taxon"]
        uniq_id = p["uniq_id"]
        csv_ids.add(sample_id)
        real_taxon = taxon if taxon != NA_VALUE else ""  # 'NA' is a placeholder, not a taxon
        if sample_id not in existing:
            stats["new_samples"] += 1
            if real_taxon:
                stats["changed_taxa"].add(real_taxon)
        elif existing[sample_id] != taxon:
            stats["changed_samples"] += 1
            if real_taxon:
                stats["changed_taxa"].add(real_taxon)
        extra = {k: v for k, v in p["extra"].items() if k not in core}
        extra_json = json.dumps(extra) if extra else None
        flags_str = ";".join(p["flags"]) or None
        sample_pk = _upsert_sample(conn, project_id, sample_id, taxon, uniq_id,
                                   extra_json, flags_str)

        for direction, filename in (("R1", p["r1"]), ("R2", p["r2"])):
            if not filename:
                continue  # empty read filename (missing_r1/r2) -> no file cataloged
            if direction == "R2" and "r1_eq_r2" in p["flags"]:
                continue  # R1 and R2 name the same file -> catalog it once (as R1)
            csv_files.add(filename)
            # A filename can only be one physical file; if the mapfile lists it under
            # two samples/directions the last upsert silently wins, so flag it.
            claim = f"{sample_id}/{direction}"
            if filename in seen_files and seen_files[filename] != claim:
                findings.append(Finding(WARN, f"file '{filename}' is listed twice "
                                        f"({seen_files[filename]} and {claim}); "
                                        "only the last row's sample/direction is kept"))
            seen_files[filename] = claim
            info = disk.get(filename) if disk else None
            rel_path = info["rel_path"] if info else os.path.join(seq_data_relpath, filename)
            size = info["size"] if info else None
            uid = info["uid"] if info else None
            name = info["name"] if info else None
            if _upsert_file(conn, project_id, sample_pk, direction, filename, rel_path,
                            size_bytes=size, owner_uid=uid, owner_name=name,
                            disk_confirmed=info is not None, proj_relpath=seq_data_relpath):
                stats["new_files"] += 1

    stats["orphan_samples"] = sorted(set(existing) - csv_ids)
    status = "warn" if (n_flagged or skipped or overall_status(findings) != "pass") else "pass"

    if prune:
        # Drop samples the corrected CSV no longer lists (files cascade), then any
        # remaining file row whose filename is no longer referenced (in-place fix).
        conn.executemany(
            "DELETE FROM samples WHERE project_id=? AND sample_id=?",
            [(project_id, sid) for sid in stats["orphan_samples"]])
        stats["pruned_samples"] = list(stats["orphan_samples"])
        if csv_files:
            # Compute stale filenames in Python (a "filename NOT IN (?,?,...)" over a
            # big project can exceed SQLITE_MAX_VARIABLE_NUMBER) and delete in chunks.
            existing_files = {r["filename"] for r in conn.execute(
                "SELECT filename FROM files WHERE project_id=?", (project_id,))}
            to_delete = sorted(existing_files - csv_files)
            for i in range(0, len(to_delete), _DELETE_CHUNK):
                conn.executemany(
                    "DELETE FROM files WHERE project_id=? AND filename=?",
                    [(project_id, fn) for fn in to_delete[i:i + _DELETE_CHUNK]])
            # filename is UNIQUE per project, so one row deleted per name.
            stats["pruned_files"] = len(to_delete)
        else:
            # An empty/all-missing mapfile references no files -- likely a transiently
            # broken mapfile rather than every file genuinely gone. Do NOT wipe the
            # project's file rows (that would be catastrophic); keep them and warn.
            findings.append(Finding(WARN,
                "prune deleted no files because the mapfile referenced none; "
                "existing file rows were kept to avoid a catastrophic wipe"))
            stats["pruned_files"] = 0

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
    all_files = []
    if data_dir:
        proj_uid, proj_name, _ = _stat_owner(data_dir, uid_cache)
        _by_basename, all_files, _coll = _discover_disk_files(
            data_dir, seqdata_root, uid_cache, _cfg_fastq_globs(conn))

    _upsert_project(conn, project_id_, source, number, description, None, project_id,
                    seqdata_root=abs_root, owner_uid=proj_uid, owner_name=proj_name,
                    metadata_status=metadata_status, metadata_detail=detail)

    stats = _empty_stats()
    stats["metadata_status"] = metadata_status
    stats["metadata_detail"] = detail
    # Catalog every physical file (keyed by rel_path), so two files sharing a basename
    # in different subdirs are BOTH kept, not collapsed to one row.
    for info in sorted(all_files, key=lambda i: i["rel_path"]):
        if _upsert_file(conn, project_id_, None, _infer_direction(info["filename"]),
                        info["filename"], info["rel_path"], size_bytes=info["size"],
                        owner_uid=info["uid"], owner_name=info["name"],
                        disk_confirmed=True, proj_relpath=project_id):
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
      - valid header, bad rows      -> samples loaded with NA-fill/skips ('flagged')
      - mapfile only (no folder)    -> samples from mapfile ('missing_seqdata')
      - folder only (no mapfile)    -> files from disk, no samples ('missing_mapfile')
      - folder + broken header      -> files from disk, no samples ('broken_mapfile')
      - valid header, no usable rows-> files from disk, no samples ('invalid_mapfile')
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
            # No usable rows loaded (all skipped / empty). Catalog the on-disk files
            # and stamp invalid_mapfile so the row doesn't masquerade as 'ok' with 0
            # samples; the detail summarizes why so the fix (edit the rows) is clear.
            stats = _ingest_diskonly(conn, pid, data_dir, seqdata_root,
                                     "invalid_mapfile", _invalid_mapfile_detail(findings))
        elif not has_dir:
            findings = [Finding(WARN, "no project folder on disk for this mapfile")] + findings
            if status == "pass":
                status = "warn"
        # Otherwise trust ingest_project's stats (metadata_status is 'ok', 'flagged',
        # or 'missing_seqdata', already set with the right detail) -- don't overwrite.
        results.append((pid_, findings, status, stats))
    return results


def prune_missing_projects(conn, seqdata_root, metadata_root, force=False):
    """Delete catalog projects that vanished from BOTH roots (folder + mapfile gone).

    Only for auto-discovery. Cascades to the project's samples/files/backups/etc.
    Safety (each refuses with skipped=True and a human-readable "reason", so a
    partial/broken mount can't wipe the catalog):
      - either root is not an existing directory (can't tell "unmounted" from "empty");
      - discovery turns up nothing at all;
      - the prune would remove more than half of this root's projects (blast-radius
        cap) -- unless force=True is passed to override.
    Only projects whose stored seqdata_root matches this run's root are eligible, so
    projects ingested from a different root are never touched.
    Returns {"pruned": [project_id, ...], "skipped": bool[, "reason": str]}.
    """
    # A missing/unmounted root reads as "empty" from a plain listdir, so refuse
    # rather than treat everything under it as vanished.
    if not (seqdata_root and os.path.isdir(seqdata_root)):
        return {"pruned": [], "skipped": True,
                "reason": f"seqdata_root is not an existing directory: {seqdata_root!r}"}
    if not (metadata_root and os.path.isdir(metadata_root)):
        return {"pruned": [], "skipped": True,
                "reason": f"metadata_root is not an existing directory: {metadata_root!r}"}
    discovered = {p["project_id"] for p in discover_projects(seqdata_root, metadata_root)}
    if not discovered:
        return {"pruned": [], "skipped": True,
                "reason": "discovery found no projects under these roots (empty or unmounted)"}
    abs_root = os.path.abspath(seqdata_root)
    in_db = [r["project_id"] for r in conn.execute(
        "SELECT project_id FROM projects WHERE seqdata_root=?", (abs_root,))]
    gone = sorted(pid for pid in in_db if pid not in discovered)
    # Blast-radius cap: removing more than half of this root's projects (or all of
    # them) likely signals a partial/broken mount, not a real cleanup. Refuse unless
    # the caller explicitly forces it.
    if not force and gone and len(gone) > len(in_db) / 2:
        return {"pruned": [], "skipped": True,
                "reason": (f"refusing to prune {len(gone)} of {len(in_db)} projects "
                           "under this root (>50%); pass force=True to override")}
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

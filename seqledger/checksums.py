"""Load and compare md5 checksums from `rclone md5sum` output (both sides)."""

import os
from datetime import date


def _decode_md5_file(path):
    """Read a checksum file, tolerating BOMs and non-UTF-8 encodings.

    Windows/PowerShell (e.g. `Get-FileHash | Out-File`) writes UTF-16 with a
    byte-order mark, which the default utf-8 reader chokes on. Sniff the BOM,
    then fall back to utf-8 and finally latin-1 (never raises).
    """
    with open(path, "rb") as f:
        raw = f.read()
    for bom, enc in (
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
        (b"\xef\xbb\xbf", "utf-8-sig"),
    ):
        if raw.startswith(bom):
            # utf-16/32 decoders keep the BOM as a leading U+FEFF; drop it.
            return raw.decode(enc).lstrip("\ufeff")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def parse_md5sum(path):
    """Parse `rclone md5sum` / coreutils md5sum output.

    Each line is "<md5>  <relpath>". Returns dict {relpath: md5}. The relpath is
    relative to the root that was hashed (the raw_sequence_data directory).
    """
    result = {}
    text = _decode_md5_file(path)
    for line in text.splitlines():
            line = line.rstrip("\n")
            if not line.strip():
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            md5, relpath = parts[0].strip(), parts[1].strip()
            # coreutils marks binary mode with a leading '*'
            if relpath.startswith("*"):
                relpath = relpath[1:]
            result[relpath] = md5
    return result


def _project_of(relpath):
    return relpath.strip("/").split("/")[0]


def load_checksums(conn, store_md5_path, pdrive_md5_path, source="backfill",
                   only_project=None):
    """Load both md5 listings, attach to files, compare, and update backups.

    Returns a summary dict.
    """
    store = parse_md5sum(store_md5_path)
    pdrive = parse_md5sum(pdrive_md5_path)

    # Index DB files by (project_id, filename).
    db_files = conn.execute(
        "SELECT file_pk, project_id, filename FROM files").fetchall()
    by_key = {(r["project_id"], r["filename"]): r["file_pk"] for r in db_files}

    today = date.today().isoformat()
    matched = 0
    warnings = []
    touched_projects = set()
    touched_pks = set()

    def _in_scope(project_id):
        return not only_project or project_id == only_project

    # Guard against a truncated/empty md5 file being loaded as if it were complete:
    # far fewer entries in scope than the catalog has cataloged files is a red flag.
    n_scope_files = sum(1 for r in db_files if _in_scope(r["project_id"]))
    for label, listing in (("Store", store), ("P-drive", pdrive)):
        n_scope_entries = sum(1 for rp in listing if _in_scope(_project_of(rp)))
        if n_scope_files > 0 and (n_scope_entries == 0
                                  or n_scope_entries * 2 < n_scope_files):
            warnings.append(
                f"{label} md5 listing is empty or much smaller than the catalog "
                f"({n_scope_entries} vs {n_scope_files}) -- truncated file?")

    # Surface the deferred basename-collision issue: two different relpaths in one
    # listing that reduce to the same (project_id, basename) fold onto one DB row.
    for label, listing in (("Store", store), ("P-drive", pdrive)):
        seen = {}
        for relpath in listing:
            project_id = _project_of(relpath)
            if not _in_scope(project_id):
                continue
            key = (project_id, os.path.basename(relpath))
            if key in seen:
                warnings.append(
                    f"{label} md5 listing: '{relpath}' and '{seen[key]}' share "
                    f"basename '{key[1]}' in project {project_id} (collision)")
            else:
                seen[key] = relpath

    # Build a per-key view combining both sides.
    all_relpaths = set(store) | set(pdrive)
    for relpath in all_relpaths:
        project_id = _project_of(relpath)
        if not _in_scope(project_id):
            continue
        filename = os.path.basename(relpath)
        key = (project_id, filename)
        file_pk = by_key.get(key)
        s_md5 = store.get(relpath)
        p_md5 = pdrive.get(relpath)

        if file_pk is None:
            warnings.append(f"{relpath}: on disk but not in metadata (orphan)")
            continue

        if s_md5 is None:
            warnings.append(f"{relpath}: missing from Store md5 listing")
        if p_md5 is None:
            warnings.append(f"{relpath}: missing from P-drive md5 listing")

        if s_md5 is not None and p_md5 is not None:
            match = 1 if s_md5 == p_md5 else 0
            if not match:
                warnings.append(f"{relpath}: Store/P-drive md5 MISMATCH")
        else:
            match = None

        conn.execute(
            """UPDATE files SET store_md5=?, pdrive_md5=?, md5=?, md5_match=?,
                                md5_source=?, date_hashed=?
               WHERE file_pk=?""",
            (s_md5, p_md5, s_md5, match, source, today, file_pk))
        matched += 1
        touched_projects.add(project_id)
        touched_pks.add(file_pk)

    # A cataloged file absent from BOTH listings is never visited above, so a
    # stale md5_match would persist and keep a backup wrongly 'verified'. Reset
    # those files to uncompared (NULL) and flag them, so _update_backup recounts.
    absent = 0
    for r in db_files:
        if not _in_scope(r["project_id"]) or r["file_pk"] in touched_pks:
            continue
        warnings.append(
            f"cataloged file '{r['filename']}' absent from both md5 listings "
            f"(backup unverified)")
        conn.execute(
            "UPDATE files SET store_md5=NULL, pdrive_md5=NULL, md5_match=NULL "
            "WHERE file_pk=?",
            (r["file_pk"],))
        touched_projects.add(r["project_id"])
        absent += 1

    for project_id in touched_projects:
        _update_backup(conn, project_id, today)

    conn.commit()
    return {"matched": matched, "warnings": warnings,
            "projects": sorted(touched_projects), "absent": absent}


def _update_backup(conn, project_id, backup_date):
    """Recompute the pdrive backup row for a project from its files."""
    cur = conn.execute(
        """SELECT COUNT(*) AS n,
                  SUM(CASE WHEN md5_match=0 THEN 1 ELSE 0 END) AS n_mismatch,
                  SUM(CASE WHEN md5_match IS NULL THEN 1 ELSE 0 END) AS n_uncompared
           FROM files WHERE project_id=?""",
        (project_id,))
    row = cur.fetchone()
    n = row["n"] or 0
    n_mismatch = row["n_mismatch"] or 0
    n_uncompared = row["n_uncompared"] or 0
    verified = 1 if (n > 0 and n_mismatch == 0 and n_uncompared == 0) else 0
    conn.execute(
        """INSERT INTO backups (project_id, location, backup_date, verified, n_files, n_mismatch)
           VALUES (?, 'pdrive', ?, ?, ?, ?)
           ON CONFLICT(project_id, location) DO UPDATE SET
             backup_date=excluded.backup_date, verified=excluded.verified,
             n_files=excluded.n_files, n_mismatch=excluded.n_mismatch""",
        (project_id, backup_date, verified, n, n_mismatch))

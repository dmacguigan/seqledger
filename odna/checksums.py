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
    cur = conn.execute("SELECT file_pk, project_id, filename FROM files")
    by_key = {(r["project_id"], r["filename"]): r["file_pk"] for r in cur.fetchall()}

    today = date.today().isoformat()
    matched = 0
    warnings = []
    touched_projects = set()

    # Build a per-key view combining both sides.
    all_relpaths = set(store) | set(pdrive)
    for relpath in all_relpaths:
        project_id = _project_of(relpath)
        if only_project and project_id != only_project:
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

    for project_id in touched_projects:
        _update_backup(conn, project_id, today)

    conn.commit()
    return {"matched": matched, "warnings": warnings, "projects": sorted(touched_projects)}


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

"""Build an rclone copy job for a hand-picked set of sample files.

The GUI "Custom table" view lets a user collect samples and export the sequence
data for them. This module turns the selected files into:
  - a disk-space estimate (sum of known file sizes), and
  - a self-contained qsub script for Hydra's I/O queue (lTIO.sq) that `rclone
    copy`s just those files, preserving their directory layout under a new dest.

rclone runs on the I/O queue because that is the only place a compute node can
reach the /store (NAS) partition. Files are grouped by their sequence-data root
so each `rclone copy` uses a --files-from list of paths relative to that root,
which recreates the same tree under the destination.
"""

import datetime
import re
import shlex

from .db import CONFIG_DEFAULTS, get_config, qsub_header


def _missing(v):
    """True for a missing value: None or a float NaN (pandas records use NaN)."""
    return v is None or v != v


def human_size(n):
    """Byte count reported in GB (e.g. 1.50 GB). None/negative -> 'unknown'."""
    if _missing(n) or n < 0:
        return "unknown"
    return f"{float(n) / 1024**3:.2f} GB"


def group_by_root(file_rows):
    """Group file rows by sequence-data root -> sorted list of rel_paths.

    file_rows: iterable of mappings with 'seqdata_root', 'rel_path' (and optional
    'size_bytes'). Rows lacking a root or rel_path cannot be copied and are
    returned separately. Returns (groups, n_unresolved) where groups is a list of
    (root, [rel_path, ...]) sorted for stable output.
    """
    by_root = {}
    n_unresolved = 0
    for r in file_rows:
        root = r["seqdata_root"]
        rel = r["rel_path"]
        if _missing(root) or _missing(rel) or not root or not rel:
            n_unresolved += 1
            continue
        by_root.setdefault(root, set()).add(rel)
    groups = [(root, sorted(rels)) for root, rels in sorted(by_root.items())]
    return groups, n_unresolved


def estimate_size(file_rows):
    """(total_bytes, n_files, n_unknown_size) for the given file rows."""
    total = 0
    n = n_unknown = 0
    for r in file_rows:
        n += 1
        size = r.get("size_bytes") if hasattr(r, "get") else r["size_bytes"]
        if _missing(size):
            n_unknown += 1
        else:
            total += int(size)
    return total, n, n_unknown


def head_groups(groups, n):
    """First n rel_paths across groups (in order), keeping the group structure."""
    out = []
    for root, rels in groups:
        if n <= 0:
            break
        out.append((root, rels[:n]))
        n -= len(rels)
    return out


def timestamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


LTIO_MAX_SLOTS = 6  # lTIO caps a user at 6 slots; requesting more never schedules.


def build_copy_script(groups, dest, transfers=4, slots=4, mem=4,
                      job_name="seqledger_rclone_copy", est_bytes=None, n_files=None,
                      rclone_module=CONFIG_DEFAULTS["rclone_module"],
                      io_queue=CONFIG_DEFAULTS["io_queue"], stamp=None):
    """Return a Hydra qsub script that rclone-copies the grouped files to dest.

    groups: list of (src_root, [rel_path, ...]) from group_by_root.
    dest:   destination directory (or rclone remote:path) to copy into.
    One `rclone copy` runs per source root, using a --files-from list so only the
    selected files are transferred while their tree under the root is preserved.
    rclone_module + io_queue come from the catalog config so another lab can retarget.
    SGE slots are clamped to LTIO_MAX_SLOTS so the job always schedules; rclone still
    uses --transfers concurrent transfers (I/O-bound, decoupled from CPU slots).
    lTIO caps: 72h wall, 8G/slot, 6 slots and 2 concurrent jobs per user.
    The log is <job_name>_<stamp>.log (stamp defaults to now) so reruns don't
    overwrite it; the job exits non-zero if any rclone copy fails.
    """
    log_name = f"{job_name}_{stamp or timestamp()}.log"
    dest = dest or "REPLACE_WITH_DESTINATION"
    slots = max(1, min(slots, LTIO_MAX_SLOTS))
    header_est = ""
    if est_bytes is not None:
        header_est = (f"# estimated transfer: {human_size(est_bytes)}"
                      + (f" across {n_files} file(s)" if n_files is not None else "") + "\n")

    blocks = []
    for i, (root, rels) in enumerate(groups, start=1):
        listing = " \\\n    ".join(shlex.quote(r) for r in rels)
        blocks.append(f"""# --- source {i} ---
SRC={shlex.quote(root)}
LIST=$(mktemp)
printf '%s\\n' \\
    {listing} > "$LIST"
rclone copy "$SRC" "$DEST" --files-from "$LIST" \\
    --transfers {transfers} --checksum || {{ rc=$?; echo "ERROR: copy from $SRC failed (rc=$rc)" >&2; rc_fail=$rc; }}
rm -f "$LIST"
""")
    body = "\n".join(blocks) if blocks else "echo 'no files selected'; exit 1\n"

    return qsub_header(job_name, log_name, io_queue, mem, slots) + f"""
{header_est}echo + `date` $JOB_NAME running on $HOSTNAME in $QUEUE with jobID=$JOB_ID
# -notify sends SIGUSR1/2 before a wall-cap kill; resubmitting the SAME script
# resumes (rclone --checksum skips files already copied).
trap 'echo "= `date` notified (approaching the 72h wall) -- stopping; resubmit this script to resume"; exit 2' SIGUSR1 SIGUSR2
module load {shlex.quote(rclone_module)}

DEST={shlex.quote(dest)}
# Fail early (before touching /store) if the destination is unset or unusable.
if [ -z "$DEST" ] || [ "$DEST" = "REPLACE_WITH_DESTINATION" ]; then
    echo "ERROR: no destination set -- edit DEST above and resubmit." >&2
    exit 1
fi
case "$DEST" in
    *:*) : ;;  # looks like an rclone remote:path -- let rclone validate it
    *)
        mkdir -p "$DEST" || {{ echo "ERROR: cannot create destination '$DEST'." >&2; exit 1; }}
        if [ ! -d "$DEST" ] || [ ! -w "$DEST" ]; then
            echo "ERROR: destination '$DEST' is not a writable directory." >&2
            exit 1
        fi ;;
esac

rc_fail=0
{body}
echo = `date` $JOB_NAME done
exit $rc_fail
"""


# --- md5 listings for backup verification ---------------------------------
# `seqledger md5-jobs` writes two scripts that make the per-side `rclone md5sum`
# listings `seqledger checksums` loads: a qsub job for Store (I/O queue) and a
# Windows batch file for the P-drive. Each lists one project at a time into its
# own file, skipping projects already listed, so a killed run resumes; the last
# step concatenates them into store.md5 / pdrive.md5.

_PROJECT_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
PDRIVE_PLACEHOLDER = "REPLACE_WITH_PDRIVE_ROOT"


def md5_projects(conn, all_projects=False):
    """Projects needing an md5 listing: rows (project_id, seqdata_root, verified, n_files,
    n_mismatch), unverified only unless all_projects."""
    loc = get_config(conn, "backup_location")
    return conn.execute(
        """SELECT p.project_id, p.seqdata_root, COALESCE(b.verified, 0) AS verified,
                  (SELECT COUNT(*) FROM files f WHERE f.project_id = p.project_id) AS n_files,
                  COALESCE(b.n_mismatch, 0) AS n_mismatch
           FROM projects p
           LEFT JOIN backups b ON b.project_id = p.project_id AND b.location = ?
           WHERE ? OR COALESCE(b.verified, 0) = 0
           ORDER BY p.project_id""", (loc, int(bool(all_projects)))).fetchall()


def _check_project_ids(project_ids):
    for pid in project_ids:
        if not _PROJECT_ID_RE.fullmatch(str(pid)):
            raise ValueError(f"project id {pid!r} is not safe for a script; "
                             "only letters, digits, . _ - are allowed")


def build_md5_store_script(projects, mem=2, job_name="seqledger_md5_store",
                           rclone_module=CONFIG_DEFAULTS["rclone_module"],
                           io_queue=CONFIG_DEFAULTS["io_queue"], stamp=None, load_cmd=""):
    """qsub script listing each (project_id, seqdata_root) with `rclone md5sum` into
    store_<project>.md5, then concatenating into store.md5. The log ends with
    load_cmd (the `seqledger checksums` command) as the next step."""
    _check_project_ids(pid for pid, _ in projects)
    log_name = f"{job_name}_{stamp or timestamp()}.log"
    blocks = []
    for pid, root in projects:
        if not root:
            raise ValueError(f"project {pid}: no sequence-data root recorded; re-run ingest")
        out = f"store_{pid}.md5"
        blocks.append(f"""if [ -s {out} ]; then echo "skip {pid}: {out} already made"; else
    echo "= `date` {pid}"
    rclone md5sum {shlex.quote(root)} --include {shlex.quote(pid + '/**')} > {out}.tmp \\
        && mv {out}.tmp {out} || {{ rc_fail=$?; rm -f {out}.tmp; echo "ERROR: {pid} failed (rc=$rc_fail)" >&2; }}
fi
""")
    body = "\n".join(blocks) if blocks else "echo 'no projects'; exit 1\n"
    return qsub_header(job_name, log_name, io_queue, mem) + f"""
echo + `date` $JOB_NAME running on $HOSTNAME in $QUEUE with jobID=$JOB_ID
# -notify sends SIGUSR1/2 before a wall-cap kill; resubmitting the SAME script
# resumes (projects with a finished store_<project>.md5 are skipped).
trap 'echo "= `date` notified (approaching the 72h wall) -- stopping; resubmit this script to resume"; exit 2' SIGUSR1 SIGUSR2
module load {shlex.quote(rclone_module)}

rc_fail=0
{body}
cat store_*.md5 > store.md5
echo = `date` $JOB_NAME done: $(wc -l < store.md5) files listed in store.md5
echo "Next: once pdrive.md5 from the Windows run is in this folder too, load both with:"
echo {shlex.quote("  " + load_cmd)}
exit $rc_fail
"""


def build_md5_pdrive_bat(project_ids, pdrive_root, rclone_exe="rclone", load_cmd=""):
    """Windows batch file listing each project under pdrive_root with `rclone md5sum`
    into pdrive_<project>.md5, then concatenating into pdrive.md5 (CRLF line ends).
    Ends by printing load_cmd (the `seqledger checksums` command) as the next step."""
    _check_project_ids(project_ids)
    lines = ["@echo off",
             f'set "ROOT={pdrive_root or PDRIVE_PLACEHOLDER}"',
             f'set "RCLONE={rclone_exe or "rclone"}"',
             f'if "%ROOT%"=="{PDRIVE_PLACEHOLDER}" (',
             '    echo ERROR: no P-drive root set -- edit ROOT at the top of this file and rerun.',
             '    exit /b 1', ')',
             'if not exist "%ROOT%\\" (',
             '    echo ERROR: P-drive root "%ROOT%" not found -- is the drive mapped?',
             '    exit /b 1', ')',
             'set FAIL=0']
    for pid in project_ids:
        out = f"pdrive_{pid}.md5"
        lines += [f'if exist "{out}" (',
                  f'    echo skip {pid}: {out} already made',
                  ') else (',
                  f'    echo {pid}',
                  f'    "%RCLONE%" md5sum "%ROOT%" --include "{pid}/**" > "{out}.tmp" '
                  f'&& move /y "{out}.tmp" "{out}" > nul || (set FAIL=1 & del "{out}.tmp" 2>nul '
                  f'& echo ERROR: {pid} failed)',
                  ')']
    lines += ['type pdrive_*.md5 > pdrive.md5 2>nul',
              'echo done: pdrive.md5 written',
              'echo Next: copy pdrive.md5 to the folder holding store.md5 on Hydra, then run there:',
              f'echo   {load_cmd}',
              'exit /b %FAIL%']
    return "\r\n".join(lines) + "\r\n"

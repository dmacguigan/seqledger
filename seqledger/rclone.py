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
import shlex

from .db import qsub_header

RCLONE_MODULE = "tools/rclone/1.66.0"


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
                      rclone_module=RCLONE_MODULE, io_queue="lTIO.sq", stamp=None):
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

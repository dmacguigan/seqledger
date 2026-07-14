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

import shlex

RCLONE_MODULE = "tools/rclone/1.66.0"


def _missing(v):
    """True for a missing value: None or a float NaN (pandas records use NaN)."""
    return v is None or v != v


def human_size(n):
    """Human-readable byte count (e.g. 1.5 GB). None/negative -> 'unknown'."""
    if _missing(n) or n < 0:
        return "unknown"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


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


def build_copy_script(groups, dest, transfers=4, slots=4, mem=4,
                      job_name="odna_rclone_copy", est_bytes=None, n_files=None):
    """Return a Hydra qsub script that rclone-copies the grouped files to dest.

    groups: list of (src_root, [rel_path, ...]) from group_by_root.
    dest:   destination directory (or rclone remote:path) to copy into.
    One `rclone copy` runs per source root, using a --files-from list so only the
    selected files are transferred while their tree under the root is preserved.
    lTIO caps: 72h wall, 8G/slot, 6 slots and 2 concurrent jobs per user.
    """
    dest = dest or "REPLACE_WITH_DESTINATION"
    mres = slots * mem
    header_est = ""
    if est_bytes is not None:
        header_est = (f"# estimated transfer: {human_size(est_bytes)}"
                      + (f" across {n_files} file(s)" if n_files is not None else "") + "\n")

    blocks = []
    for i, (root, rels) in enumerate(groups, start=1):
        listing = "\n".join(rels)
        blocks.append(f"""# --- source {i}: {root} ---
SRC={shlex.quote(root)}
LIST=$(mktemp)
cat > "$LIST" <<'FILES'
{listing}
FILES
rclone copy "$SRC" "$DEST" --files-from "$LIST" \\
    --transfers {transfers} --checksum --progress
rm -f "$LIST"
""")
    body = "\n".join(blocks) if blocks else "echo 'no files selected'; exit 1\n"

    return f"""#!/bin/bash
#$ -N {job_name}
#$ -o {job_name}.log
#$ -j y
#$ -terse
#$ -notify
#$ -pe mthread {slots}
#$ -q lTIO.sq -l ioq
#$ -l mres={mres}G,h_data={mem}G,h_vmem={mem}G
#$ -S /bin/bash
#$ -cwd

{header_est}echo + `date` $JOB_NAME running on $HOSTNAME in $QUEUE with jobID=$JOB_ID
module load {RCLONE_MODULE}

DEST={shlex.quote(dest)}
mkdir -p "$DEST"

{body}
echo = `date` $JOB_NAME done
"""

"""Shared fixture builders for tests."""

import gzip
import os


def make_project(root, project_dir, mapfile_name, rows, disk_files=None, header=None):
    """Create a project on disk: data dir with fastq.gz + a mapfile CSV.

    - root: base dir (acts as raw_sequence_data root).
    - project_dir: relative data dir (may be nested).
    - rows: list of tuples (ID, R1, R2, Taxon, UniqID[, *extra]).
    - disk_files: filenames to actually create in the data dir. Defaults to all
      R1/R2 in rows.
    Returns (mapfile_path, project_dir).
    """
    data_dir = os.path.join(root, project_dir)
    os.makedirs(data_dir, exist_ok=True)

    if disk_files is None:
        disk_files = []
        for r in rows:
            disk_files.extend([r[1], r[2]])
    for fn in disk_files:
        with gzip.open(os.path.join(data_dir, fn), "wb") as f:
            f.write(b"@read\nACGT\n+\nIIII\n")

    header = header or ["ID", "R1", "R2", "Taxon", "UniqID"]
    mapfile_path = os.path.join(root, mapfile_name)
    with open(mapfile_path, "w") as f:
        f.write(",".join(header) + "\n")
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")
    return mapfile_path, project_dir


def write_map_file(root, entries):
    """Write the two-column map file (metadata csv, data dir)."""
    path = os.path.join(root, "map_file.txt")
    with open(path, "w") as f:
        f.write("metadata datadir\n")
        for mapfile, datadir in entries:
            f.write(f"{mapfile} {datadir}\n")
    return path


def write_md5(path, entries):
    """Write an rclone-md5sum-style file. entries: list of (md5, relpath)."""
    with open(path, "w") as f:
        for md5, relpath in entries:
            f.write(f"{md5}  {relpath}\n")
    return path

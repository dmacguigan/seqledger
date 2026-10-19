"""Shared fixture builders for tests."""

import gzip
import os

from seqledger import db as odb
from seqledger import ingest as oingest


def write_gz(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wb") as f:
        f.write(b"@read\nACGT\n+\nIIII\n")


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
        write_gz(os.path.join(data_dir, fn))

    header = header or ["ID", "R1", "R2", "Taxon", "UniqID"]
    mapfile_path = os.path.join(root, mapfile_name)
    with open(mapfile_path, "w") as f:
        f.write(",".join(header) + "\n")
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")
    return mapfile_path, project_dir


def ingested_project(tmp_path, rows, disk_files=None):
    """One ingested project 'genohub-1_X'. Returns (conn, root)."""
    root = str(tmp_path / "raw_sequence_data")
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows, disk_files=disk_files)
    conn = odb.connect(str(tmp_path / "cat.db"))
    odb.init_db(conn)
    oingest.ingest_tree(conn, root, root)
    return conn, root


def write_md5(path, entries):
    """Write an rclone-md5sum-style file. entries: list of (md5, relpath)."""
    with open(path, "w") as f:
        for md5, relpath in entries:
            f.write(f"{md5}  {relpath}\n")
    return path

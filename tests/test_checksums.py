import os

import pytest

from seqledger import db as odb
from seqledger import ingest as oingest
from seqledger import checksums as ochecksums
from helpers import make_project, write_map_file, write_md5


def _setup_project(tmp_path):
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    rows = [
        ("i1", "i1_1.fastq.gz", "i1_2.fastq.gz", "Gadus", "U1"),
        ("i2", "i2_1.fastq.gz", "i2_2.fastq.gz", "Gadus", "U2"),
    ]
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows)
    map_file = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])
    conn = odb.connect(os.path.join(tmp_path, "cat.db"))
    odb.init_db(conn)
    oingest.ingest_map_file(conn, map_file, seqdata_root=root)
    return conn, root


FILES = ["i1_1.fastq.gz", "i1_2.fastq.gz", "i2_1.fastq.gz", "i2_2.fastq.gz"]


def _md5_entries(overrides=None, extra=None):
    overrides = overrides or {}
    entries = []
    for fn in FILES:
        entries.append((overrides.get(fn, "a" * 32), f"genohub-1_X/{fn}"))
    if extra:
        entries.extend(extra)
    return entries


def test_matching_checksums_verify_backup(tmp_path):
    conn, _ = _setup_project(tmp_path)
    store = write_md5(str(tmp_path / "store.md5"), _md5_entries())
    pdrive = write_md5(str(tmp_path / "pdrive.md5"), _md5_entries())

    summary = ochecksums.load_checksums(conn, store, pdrive)
    assert summary["matched"] == 4

    matches = conn.execute("SELECT COUNT(*) FROM files WHERE md5_match=1").fetchone()[0]
    assert matches == 4
    backup = conn.execute(
        "SELECT verified, n_files, n_mismatch FROM backups WHERE project_id='genohub-1_X'"
    ).fetchone()
    assert (backup["verified"], backup["n_files"], backup["n_mismatch"]) == (1, 4, 0)


def test_mismatch_flags_backup(tmp_path):
    conn, _ = _setup_project(tmp_path)
    store = write_md5(str(tmp_path / "store.md5"), _md5_entries())
    pdrive = write_md5(str(tmp_path / "pdrive.md5"),
                       _md5_entries(overrides={"i2_2.fastq.gz": "b" * 32}))

    summary = ochecksums.load_checksums(conn, store, pdrive)
    assert any("MISMATCH" in w for w in summary["warnings"])

    bad = conn.execute(
        "SELECT filename FROM files WHERE md5_match=0").fetchall()
    assert [r["filename"] for r in bad] == ["i2_2.fastq.gz"]
    backup = conn.execute(
        "SELECT verified, n_mismatch FROM backups WHERE project_id='genohub-1_X'").fetchone()
    assert (backup["verified"], backup["n_mismatch"]) == (0, 1)


def test_orphan_in_md5_listing_warns(tmp_path):
    conn, _ = _setup_project(tmp_path)
    extra = [("a" * 32, "genohub-1_X/stray.fastq.gz")]
    store = write_md5(str(tmp_path / "store.md5"), _md5_entries(extra=extra))
    pdrive = write_md5(str(tmp_path / "pdrive.md5"), _md5_entries(extra=extra))

    summary = ochecksums.load_checksums(conn, store, pdrive)
    assert any("orphan" in w and "stray.fastq.gz" in w for w in summary["warnings"])


def test_file_absent_from_both_listings_unverifies_backup(tmp_path):
    # #5 regression: a cataloged file that vanishes from BOTH md5 listings used
    # to keep its stale md5_match=1, leaving the backup wrongly 'verified'.
    conn, _ = _setup_project(tmp_path)

    # First, a complete pair verifies the backup.
    store = write_md5(str(tmp_path / "store.md5"), _md5_entries())
    pdrive = write_md5(str(tmp_path / "pdrive.md5"), _md5_entries())
    ochecksums.load_checksums(conn, store, pdrive)
    backup = conn.execute(
        "SELECT verified FROM backups WHERE project_id='genohub-1_X'").fetchone()
    assert backup["verified"] == 1

    # Now one file is absent from both listings (e.g. deleted before re-hashing).
    present = [e for e in _md5_entries() if not e[1].endswith("i2_2.fastq.gz")]
    store2 = write_md5(str(tmp_path / "store2.md5"), present)
    pdrive2 = write_md5(str(tmp_path / "pdrive2.md5"), present)
    summary = ochecksums.load_checksums(conn, store2, pdrive2)

    # The vanished file is reset to uncompared and flagged.
    row = conn.execute(
        "SELECT md5_match, store_md5, pdrive_md5 FROM files "
        "WHERE filename='i2_2.fastq.gz'").fetchone()
    assert row["md5_match"] is None
    assert row["store_md5"] is None and row["pdrive_md5"] is None
    assert summary["absent"] == 1
    assert any("i2_2.fastq.gz" in w and "absent from both" in w
               for w in summary["warnings"])

    # ...so the backup is no longer verified.
    backup = conn.execute(
        "SELECT verified FROM backups WHERE project_id='genohub-1_X'").fetchone()
    assert backup["verified"] != 1


def test_empty_store_listing_warns_and_leaves_status_incomplete(tmp_path):
    # #21 regression: an empty/truncated listing must not pass silently.
    conn, _ = _setup_project(tmp_path)
    store = write_md5(str(tmp_path / "store.md5"), [])  # truncated to nothing
    pdrive = write_md5(str(tmp_path / "pdrive.md5"), _md5_entries())

    summary = ochecksums.load_checksums(conn, store, pdrive, force=True)

    assert any("Store md5 listing is empty" in w for w in summary["warnings"])
    # No file can be verified with the store side missing; all stay uncompared.
    n_null = conn.execute(
        "SELECT COUNT(*) FROM files WHERE md5_match IS NULL").fetchone()[0]
    assert n_null == 4
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE md5_match=1").fetchone()[0] == 0
    backup = conn.execute(
        "SELECT verified FROM backups WHERE project_id='genohub-1_X'").fetchone()
    assert backup["verified"] != 1


def test_checksum_matches_by_relpath_not_basename(tmp_path):
    # Two files sharing a basename in different subdirs must each get their OWN md5
    # verdict -- matching on rel_path, not basename, so no cross-contamination.
    import gzip
    root = str(tmp_path / "raw_sequence_data")
    pid = "genohub-2_COLL"
    for sub in ("laneA", "laneB"):
        d = os.path.join(root, pid, sub)
        os.makedirs(d, exist_ok=True)
        with gzip.open(os.path.join(d, "x_1.fastq.gz"), "wb") as f:
            f.write(b"@r\nACGT\n+\nIIII\n")
    conn = odb.connect(os.path.join(tmp_path, "cat.db"))
    odb.init_db(conn)
    oingest.ingest_tree(conn, root, root)  # diskonly -> both files cataloged
    store = write_md5(str(tmp_path / "store.md5"), [
        ("a" * 32, f"{pid}/laneA/x_1.fastq.gz"),
        ("b" * 32, f"{pid}/laneB/x_1.fastq.gz")])
    pdrive = write_md5(str(tmp_path / "pdrive.md5"), [
        ("a" * 32, f"{pid}/laneA/x_1.fastq.gz"),   # laneA: Store == P-drive
        ("c" * 32, f"{pid}/laneB/x_1.fastq.gz")])  # laneB: mismatch
    ochecksums.load_checksums(conn, store, pdrive, source="backfill")
    got = {r["rel_path"]: r["md5_match"] for r in conn.execute(
        "SELECT rel_path, md5_match FROM files WHERE project_id=?", (pid,))}
    assert got[f"{pid}/laneA/x_1.fastq.gz"] == 1
    assert got[f"{pid}/laneB/x_1.fastq.gz"] == 0   # correctly mismatched, no crosstalk


def _setup_two_projects(tmp_path):
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    pairs = []
    for pid in ("projA", "projB"):
        rows = [("i1", "i1_1.fastq.gz", "i1_2.fastq.gz", "Gadus", f"{pid}U1")]
        make_project(root, pid, f"{pid}_mapfile.csv", rows)
        pairs.append((f"{pid}_mapfile.csv", pid))
    conn = odb.connect(os.path.join(tmp_path, "cat.db"))
    odb.init_db(conn)
    oingest.ingest_map_file(conn, write_map_file(root, pairs), seqdata_root=root)
    full = [("a" * 32, f"{pid}/i1_{d}.fastq.gz")
            for pid in ("projA", "projB") for d in (1, 2)]
    s = write_md5(str(tmp_path / "s0.md5"), full)
    p = write_md5(str(tmp_path / "p0.md5"), full)
    ochecksums.load_checksums(conn, s, p)
    return conn


def _state(conn, pid):
    m = [r[0] for r in conn.execute(
        "SELECT md5_match FROM files WHERE project_id=? ORDER BY file_pk", (pid,))]
    v = conn.execute(
        "SELECT verified FROM backups WHERE project_id=?", (pid,)).fetchone()[0]
    return m, v


def _projA_only(tmp_path):
    entries = [("a" * 32, f"projA/i1_{d}.fastq.gz") for d in (1, 2)]
    return (write_md5(str(tmp_path / "sA.md5"), entries),
            write_md5(str(tmp_path / "pA.md5"), entries))


def test_listing_for_one_project_leaves_others_untouched(tmp_path):
    conn = _setup_two_projects(tmp_path)
    assert _state(conn, "projB") == ([1, 1], 1)
    s, p = _projA_only(tmp_path)
    summary = ochecksums.load_checksums(conn, s, p)
    assert summary["absent"] == 0
    assert _state(conn, "projA") == ([1, 1], 1)
    assert _state(conn, "projB") == ([1, 1], 1)


def test_full_reset_resets_projects_absent_from_listing(tmp_path):
    conn = _setup_two_projects(tmp_path)
    s, p = _projA_only(tmp_path)
    summary = ochecksums.load_checksums(conn, s, p, full_reset=True, force=True)
    assert summary["absent"] == 2
    assert _state(conn, "projA") == ([1, 1], 1)
    assert _state(conn, "projB") == ([None, None], 0)


def test_truncated_listing_aborts_without_commit_unless_force(tmp_path):
    conn = _setup_two_projects(tmp_path)
    s = write_md5(str(tmp_path / "s.md5"), [])
    p = write_md5(str(tmp_path / "p.md5"),
                  [("a" * 32, f"projA/i1_{d}.fastq.gz") for d in (1, 2)])
    with pytest.raises(ValueError, match="--force"):
        ochecksums.load_checksums(conn, s, p)
    conn.rollback()
    assert _state(conn, "projA") == ([1, 1], 1)
    ochecksums.load_checksums(conn, s, p, force=True)
    assert _state(conn, "projA") == ([None, None], 0)
    assert _state(conn, "projB") == ([1, 1], 1)


def test_uppercase_md5_matches_lowercase(tmp_path):
    conn, _ = _setup_project(tmp_path)
    store = write_md5(str(tmp_path / "store.md5"), _md5_entries())
    pdrive = write_md5(str(tmp_path / "pdrive.md5"),
                       [(m.upper(), rp) for m, rp in _md5_entries()])
    ochecksums.load_checksums(conn, store, pdrive)
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE md5_match=1").fetchone()[0] == 4


def test_dot_slash_and_backslash_paths_match(tmp_path):
    conn, _ = _setup_project(tmp_path)
    store = write_md5(str(tmp_path / "store.md5"),
                      [(m, "./" + rp) for m, rp in _md5_entries()])
    pdrive = write_md5(str(tmp_path / "pdrive.md5"),
                       [(m, "*" + rp.replace("/", "\\")) for m, rp in _md5_entries()])
    summary = ochecksums.load_checksums(conn, store, pdrive)
    assert summary["matched"] == 4
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE md5_match=1").fetchone()[0] == 4


def test_backup_location_config_honored(tmp_path):
    from seqledger import query as oquery
    conn, _ = _setup_project(tmp_path)
    odb.set_config(conn, "backup_location", "nas")
    entries = _md5_entries()
    ochecksums.load_checksums(conn, write_md5(str(tmp_path / "s.md5"), entries),
                              write_md5(str(tmp_path / "p.md5"), entries))
    locs = [r[0] for r in conn.execute("SELECT location FROM backups")]
    assert locs == ["nas"]
    assert oquery.unbacked_projects(conn) == []
    odb.set_config(conn, "backup_location", "pdrive")
    assert [r["project_id"] for r in oquery.unbacked_projects(conn)] == ["genohub-1_X"]

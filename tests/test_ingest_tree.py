"""Auto-discovery ingest: pair project folders with <project>_mapfile.csv."""

import os

from seqledger import ingest as oingest
from helpers import make_project, write_gz


def _roots(tmp_path):
    """seqdata + metadata roots (same dir, matching the on-disk convention)."""
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    return root, root


def test_tree_ingests_folder_plus_mapfile(tmp_path, fresh_db):
    seq, meta = _roots(tmp_path)
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    make_project(seq, "genohub-1_X", "genohub-1_X_mapfile.csv", rows)

    conn = fresh_db
    results = oingest.ingest_tree(conn, seq, meta)

    assert [r[0] for r in results] == ["genohub-1_X"]
    assert results[0][2] == "pass"
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2
    assert conn.execute(
        "SELECT metadata_status FROM projects").fetchone()["metadata_status"] == "ok"


def test_tree_finds_nested_fastq(tmp_path, fresh_db):
    seq, meta = _roots(tmp_path)
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    # mapfile lists bare basenames; files live in a nested subdir on disk.
    make_project(seq, "genohub-1_X", "genohub-1_X_mapfile.csv", rows, disk_files=[])
    write_gz(os.path.join(seq, "genohub-1_X", "lane1", "s1_1.fastq.gz"))
    write_gz(os.path.join(seq, "genohub-1_X", "lane1", "s1_2.fastq.gz"))

    conn = fresh_db
    oingest.ingest_tree(conn, seq, meta)

    rel = conn.execute(
        "SELECT rel_path FROM files WHERE filename='s1_1.fastq.gz'").fetchone()["rel_path"]
    assert rel == os.path.join("genohub-1_X", "lane1", "s1_1.fastq.gz")
    # size captured -> file was actually located on disk
    assert conn.execute(
        "SELECT size_bytes FROM files WHERE filename='s1_1.fastq.gz'").fetchone()[0] > 0


def test_tree_missing_mapfile_catalogs_disk_files_only(tmp_path, fresh_db):
    seq, meta = _roots(tmp_path)
    # A project folder with fastq but NO mapfile in the metadata dir.
    write_gz(os.path.join(seq, "genohub-9_Z", "a_1.fastq.gz"))
    write_gz(os.path.join(seq, "genohub-9_Z", "a_2.fastq.gz"))

    conn = fresh_db
    results = oingest.ingest_tree(conn, seq, meta)

    assert results[0][3]["metadata_status"] == "missing_mapfile"
    row = conn.execute(
        "SELECT metadata_status FROM projects WHERE project_id='genohub-9_Z'").fetchone()
    assert row["metadata_status"] == "missing_mapfile"
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0
    # files cataloged with no sample and a best-effort direction guess
    files = conn.execute(
        "SELECT filename, sample_pk, direction FROM files ORDER BY filename").fetchall()
    assert [f["filename"] for f in files] == ["a_1.fastq.gz", "a_2.fastq.gz"]
    assert all(f["sample_pk"] is None for f in files)
    assert [f["direction"] for f in files] == ["R1", "R2"]


def test_tree_missing_seqdata_catalogs_samples_only(tmp_path, fresh_db):
    seq, meta = _roots(tmp_path)
    # A mapfile with no matching project folder on disk.
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")]
    make_project(seq, "genohub-7_M", "genohub-7_M_mapfile.csv", rows, disk_files=[])
    # remove the (empty) folder make_project created, so no folder exists
    os.rmdir(os.path.join(seq, "genohub-7_M"))

    conn = fresh_db
    results = oingest.ingest_tree(conn, seq, meta)

    assert results[0][3]["metadata_status"] == "missing_seqdata"
    assert conn.execute(
        "SELECT metadata_status FROM projects").fetchone()["metadata_status"] == "missing_seqdata"
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1


def test_tree_broken_mapfile_flags_and_catalogs_disk(tmp_path, fresh_db):
    seq, meta = _roots(tmp_path)
    # Bad header (missing required columns) but a real folder with fastq.
    write_gz(os.path.join(seq, "genohub-3_B", "x_1.fastq.gz"))
    with open(os.path.join(seq, "genohub-3_B_mapfile.csv"), "w") as f:
        f.write("ID,Sample,Notes\n")
        f.write("s1,foo,bar\n")

    conn = fresh_db
    results = oingest.ingest_tree(conn, seq, meta)

    assert results[0][2] == "fail"
    assert results[0][3]["metadata_status"] == "broken_mapfile"
    row = conn.execute("SELECT metadata_status, metadata_detail FROM projects").fetchone()
    assert row["metadata_status"] == "broken_mapfile"
    assert "ID,R1,R2,Taxon,UniqID" in row["metadata_detail"]
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1


def test_tree_valid_header_invalid_rows_flags_not_ok(tmp_path, fresh_db):
    # A VALID header whose rows have empty fields (a control row with empty UniqID)
    # must NOT be silently 'ok': the control row loads with UniqID NA-filled, the
    # sample is flagged, and the project is 'flagged'.
    seq, meta = _roots(tmp_path)
    write_gz(os.path.join(seq, "genohub-9_C", "s1_1.fastq.gz"))
    write_gz(os.path.join(seq, "genohub-9_C", "s1_2.fastq.gz"))
    write_gz(os.path.join(seq, "genohub-9_C", "c_1.fastq.gz"))
    write_gz(os.path.join(seq, "genohub-9_C", "c_2.fastq.gz"))
    with open(os.path.join(meta, "genohub-9_C_mapfile.csv"), "w") as f:
        f.write("ID,R1,R2,Taxon,UniqID\n")
        f.write("s1,s1_1.fastq.gz,s1_2.fastq.gz,Gadus,U1\n")
        f.write("Amp_neg_Plate1,c_1.fastq.gz,c_2.fastq.gz,control,\n")  # empty UniqID -> NA

    conn = fresh_db
    oingest.ingest_tree(conn, seq, meta)

    row = conn.execute(
        "SELECT metadata_status FROM projects WHERE project_id='genohub-9_C'").fetchone()
    assert row["metadata_status"] == "flagged"      # not 'ok', not 'invalid_mapfile'
    # BOTH samples loaded; the control row's UniqID was NA-filled + flagged
    assert conn.execute(
        "SELECT COUNT(*) FROM samples WHERE project_id='genohub-9_C'").fetchone()[0] == 2
    ctrl = conn.execute(
        "SELECT uniq_id, flags FROM samples WHERE sample_id='Amp_neg_Plate1'").fetchone()
    assert ctrl["uniq_id"] == "NA"
    assert "na_uniqid" in ctrl["flags"]
    # the clean sample has no flags
    clean = conn.execute("SELECT flags FROM samples WHERE sample_id='s1'").fetchone()
    assert clean["flags"] is None


def test_tree_all_rows_unusable_is_invalid_mapfile(tmp_path, fresh_db):
    # If NO row is loadable (all empty IDs), it's 'invalid_mapfile' with files only.
    seq, meta = _roots(tmp_path)
    write_gz(os.path.join(seq, "genohub-8_D", "x_1.fastq.gz"))
    with open(os.path.join(meta, "genohub-8_D_mapfile.csv"), "w") as f:
        f.write("ID,R1,R2,Taxon,UniqID\n")
        f.write(",x_1.fastq.gz,x_2.fastq.gz,Gadus,U1\n")  # empty ID -> skipped
    conn = fresh_db
    oingest.ingest_tree(conn, seq, meta)
    row = conn.execute(
        "SELECT metadata_status FROM projects WHERE project_id='genohub-8_D'").fetchone()
    assert row["metadata_status"] == "invalid_mapfile"
    assert conn.execute(
        "SELECT COUNT(*) FROM samples WHERE project_id='genohub-8_D'").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE project_id='genohub-8_D'").fetchone()[0] == 1


def test_discover_projects_unions_both_sides(tmp_path):
    seq, meta = _roots(tmp_path)
    os.makedirs(os.path.join(seq, "onlyfolder"))
    open(os.path.join(meta, "onlymap_mapfile.csv"), "w").close()
    make_project(seq, "both", "both_mapfile.csv",
                 [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")])

    found = {p["project_id"]: p for p in oingest.discover_projects(seq, meta)}
    assert set(found) == {"onlyfolder", "onlymap", "both"}
    assert found["onlyfolder"]["mapfile"] is None
    assert found["onlymap"]["data_dir"] is None
    assert found["both"]["data_dir"] and found["both"]["mapfile"]


def test_prune_missing_projects_deletes_vanished(tmp_path, fresh_db):
    seq, meta = _roots(tmp_path)
    make_project(seq, "genohub-1_A", "genohub-1_A_mapfile.csv",
                 [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")])
    make_project(seq, "genohub-2_B", "genohub-2_B_mapfile.csv",
                 [("s2", "s2_1.fastq.gz", "s2_2.fastq.gz", "Gadus", "U2")])
    conn = fresh_db
    oingest.ingest_tree(conn, seq, meta)
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 2

    # B vanishes from both roots (folder + mapfile removed).
    shutil = __import__("shutil")
    shutil.rmtree(os.path.join(seq, "genohub-2_B"))
    os.remove(os.path.join(meta, "genohub-2_B_mapfile.csv"))

    res = oingest.prune_missing_projects(conn, seq, meta)
    assert res["skipped"] is False
    assert res["pruned"] == ["genohub-2_B"]
    # project + its samples/files cascade-deleted; A untouched
    assert [r["project_id"] for r in conn.execute("SELECT project_id FROM projects")] == \
        ["genohub-1_A"]
    assert conn.execute(
        "SELECT COUNT(*) FROM samples WHERE project_id='genohub-2_B'").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE project_id='genohub-2_B'").fetchone()[0] == 0


def test_prune_missing_projects_refuses_when_nothing_discovered(tmp_path, fresh_db):
    seq, meta = _roots(tmp_path)
    make_project(seq, "genohub-1_A", "genohub-1_A_mapfile.csv",
                 [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")])
    conn = fresh_db
    oingest.ingest_tree(conn, seq, meta)

    # Simulate an unmounted / empty root: discovery finds nothing -> must NOT prune.
    empty = str(tmp_path / "empty_root")
    os.makedirs(empty, exist_ok=True)
    res = oingest.prune_missing_projects(conn, empty, empty)
    assert res["skipped"] is True
    assert res["pruned"] == []
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1  # kept


def test_prune_missing_projects_ignores_other_roots(tmp_path, fresh_db):
    seq, meta = _roots(tmp_path)
    make_project(seq, "genohub-1_A", "genohub-1_A_mapfile.csv",
                 [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")])
    conn = fresh_db
    oingest.ingest_tree(conn, seq, meta)

    # A project stored under a DIFFERENT seqdata_root must not be pruned by a run
    # over this root, even though it isn't discovered here.
    conn.execute("INSERT INTO projects (project_id, seqdata_root) VALUES (?, ?)",
                 ("other-proj", "/some/other/root"))
    conn.commit()
    res = oingest.prune_missing_projects(conn, seq, meta)
    assert res["pruned"] == []  # nothing under this root vanished
    assert conn.execute(
        "SELECT COUNT(*) FROM projects WHERE project_id='other-proj'").fetchone()[0] == 1


def test_reingest_metadata_only_keeps_nested_rel_path(tmp_path, fresh_db):
    # First ingest sees the fastq nested on disk -> a nested rel_path is recorded.
    seq, meta = _roots(tmp_path)
    make_project(seq, "genohub-1_X", "genohub-1_X_mapfile.csv",
                 [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")], disk_files=[])
    write_gz(os.path.join(seq, "genohub-1_X", "lane1", "s1_1.fastq.gz"))
    write_gz(os.path.join(seq, "genohub-1_X", "lane1", "s1_2.fastq.gz"))
    conn = fresh_db
    oingest.ingest_tree(conn, seq, meta)
    nested = os.path.join("genohub-1_X", "lane1", "s1_1.fastq.gz")
    assert conn.execute(
        "SELECT rel_path FROM files WHERE filename='s1_1.fastq.gz'").fetchone()["rel_path"] == nested

    # Re-ingest metadata-only (disk unreachable -> flat guess). The correct nested
    # rel_path must be preserved, not clobbered by the flat guess (#3).
    oingest.ingest_project(conn, os.path.join(meta, "genohub-1_X_mapfile.csv"),
                           "genohub-1_X")  # no seqdata_root
    assert conn.execute(
        "SELECT rel_path FROM files WHERE filename='s1_1.fastq.gz'").fetchone()["rel_path"] == nested


def test_prune_refuses_when_root_missing(tmp_path, fresh_db):
    # An unmounted / nonexistent root reads as "empty"; prune must refuse, not wipe (#1).
    seq, meta = _roots(tmp_path)
    make_project(seq, "genohub-1_A", "genohub-1_A_mapfile.csv",
                 [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")])
    conn = fresh_db
    oingest.ingest_tree(conn, seq, meta)

    missing = str(tmp_path / "not_mounted")  # never created
    res = oingest.prune_missing_projects(conn, missing, missing)
    assert res["skipped"] is True and res["pruned"] == []
    assert "reason" in res
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1  # kept


def test_prune_blast_radius_cap_refuses_without_force(tmp_path, fresh_db):
    # Removing >50% of a root's projects likely means a partial mount; refuse unless
    # force=True is passed (#1).
    seq, meta = _roots(tmp_path)
    for n in (1, 2, 3):
        make_project(seq, f"genohub-{n}_P", f"genohub-{n}_P_mapfile.csv",
                     [(f"s{n}", f"s{n}_1.fastq.gz", f"s{n}_2.fastq.gz", "Gadus", f"U{n}")])
    conn = fresh_db
    oingest.ingest_tree(conn, seq, meta)
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 3

    import shutil
    for n in (2, 3):  # two of three vanish (>50%)
        shutil.rmtree(os.path.join(seq, f"genohub-{n}_P"))
        os.remove(os.path.join(meta, f"genohub-{n}_P_mapfile.csv"))

    res = oingest.prune_missing_projects(conn, seq, meta)
    assert res["skipped"] is True and res["pruned"] == []
    assert "reason" in res
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 3  # kept

    # force=True overrides the cap.
    res = oingest.prune_missing_projects(conn, seq, meta, force=True)
    assert res["skipped"] is False
    assert sorted(res["pruned"]) == ["genohub-2_P", "genohub-3_P"]
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_tree_reingest_upgrades_missing_mapfile_to_ok(tmp_path, fresh_db):
    seq, meta = _roots(tmp_path)
    write_gz(os.path.join(seq, "genohub-1_X", "s1_1.fastq.gz"))
    write_gz(os.path.join(seq, "genohub-1_X", "s1_2.fastq.gz"))
    conn = fresh_db
    oingest.ingest_tree(conn, seq, meta)
    assert conn.execute(
        "SELECT metadata_status FROM projects").fetchone()["metadata_status"] == "missing_mapfile"

    # Add the mapfile and re-ingest: project flips to ok, files link to the sample.
    with open(os.path.join(meta, "genohub-1_X_mapfile.csv"), "w") as f:
        f.write("ID,R1,R2,Taxon,UniqID\n")
        f.write("s1,s1_1.fastq.gz,s1_2.fastq.gz,Gadus,U1\n")
    oingest.ingest_tree(conn, seq, meta)

    assert conn.execute(
        "SELECT metadata_status FROM projects").fetchone()["metadata_status"] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
    linked = conn.execute(
        "SELECT COUNT(*) FROM files WHERE sample_pk IS NOT NULL").fetchone()[0]
    assert linked == 2


def test_reingest_case_insensitive_header_loads_samples(tmp_path, fresh_db):
    # A mapfile with a lowercase header (id,r1,r2,taxon,uniqid) is accepted
    # case-insensitively AND its rows load -- not silently dropped as invalid.
    root, meta = _roots(tmp_path)
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus morhua", "USNM 1")]
    make_project(root, "genohub-9_CI", "genohub-9_CI_mapfile.csv", rows,
                 header=["id", "r1", "r2", "taxon", "uniqid"])
    conn = fresh_db
    res = oingest.ingest_tree(conn, root, meta)
    pid, findings, status, stats = res[0]
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1, findings
    mstatus = conn.execute(
        "SELECT metadata_status FROM projects WHERE project_id=?", (pid,)).fetchone()[0]
    assert mstatus == "ok", (mstatus, findings)
    # core columns are not re-captured into extra_json despite lowercase header
    extra = conn.execute("SELECT extra_json FROM samples").fetchone()[0]
    assert extra is None


def test_diskonly_same_basename_both_cataloged(tmp_path, fresh_db):
    # A project folder with NO mapfile and two files sharing a basename in different
    # subdirs -> BOTH cataloged as distinct rows keyed by rel_path (not collapsed).
    root, meta = _roots(tmp_path)
    pid = "genohub-9_COLL"
    write_gz(os.path.join(root, pid, "laneA", "x_1.fastq.gz"))
    write_gz(os.path.join(root, pid, "laneB", "x_1.fastq.gz"))
    conn = fresh_db
    oingest.ingest_tree(conn, root, meta)  # no mapfile -> diskonly cataloging
    relpaths = {r["rel_path"] for r in conn.execute(
        "SELECT rel_path FROM files WHERE project_id=?", (pid,))}
    assert relpaths == {f"{pid}/laneA/x_1.fastq.gz", f"{pid}/laneB/x_1.fastq.gz"}


def test_reingest_flatguess_moves_to_nested_no_dup(tmp_path, fresh_db):
    # Metadata-only ingest records a flat-guess rel_path; when the file later arrives
    # nested on disk, a disk-backed re-ingest MOVES that row to the real path instead
    # of forking a duplicate (and does not collapse genuinely distinct files).
    from helpers import make_project, write_gz
    root, meta = _roots(tmp_path)
    pid = "genohub-8_FG"
    rows = [("s1", "x_1.fastq.gz", "x_2.fastq.gz", "Gadus", "U1")]
    make_project(root, pid, f"{pid}_mapfile.csv", rows, disk_files=[])  # mapfile, no files yet
    conn = fresh_db
    oingest.ingest_tree(conn, root, meta)
    rp = {r["rel_path"] for r in conn.execute(
        "SELECT rel_path FROM files WHERE project_id=?", (pid,))}
    assert rp == {f"{pid}/x_1.fastq.gz", f"{pid}/x_2.fastq.gz"}  # flat guesses

    write_gz(os.path.join(root, pid, "run1", "x_1.fastq.gz"))
    write_gz(os.path.join(root, pid, "run1", "x_2.fastq.gz"))
    oingest.ingest_tree(conn, root, meta)  # disk-backed re-ingest
    rp2 = {r["rel_path"] for r in conn.execute(
        "SELECT rel_path FROM files WHERE project_id=?", (pid,))}
    assert rp2 == {f"{pid}/run1/x_1.fastq.gz", f"{pid}/run1/x_2.fastq.gz"}  # moved
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE project_id=?", (pid,)).fetchone()[0] == 2  # no dup


def _write_mapfile(meta, pid, lines, header="ID,R1,R2,Taxon,UniqID"):
    with open(os.path.join(meta, f"{pid}_mapfile.csv"), "w") as f:
        f.write(header + "\n")
        for line in lines:
            f.write(line + "\n")


def _links(conn):
    return {r["filename"]: (r["sample_pk"], r["direction"]) for r in conn.execute(
        "SELECT filename, sample_pk, direction FROM files")}


def _linked_project(tmp_path, conn):
    # Names whose R1/R2 the disk-only guess cannot infer, so a lost direction shows.
    root, meta = _roots(tmp_path)
    pid = "genohub-1_X"
    write_gz(os.path.join(root, pid, "s1_fwd.fastq.gz"))
    write_gz(os.path.join(root, pid, "s1_rev.fastq.gz"))
    _write_mapfile(meta, pid, ["s1,s1_fwd.fastq.gz,s1_rev.fastq.gz,Gadus,U1"])
    oingest.ingest_tree(conn, root, meta)
    return root, meta, pid, conn


def test_reingest_missing_mapfile_keeps_sample_links(tmp_path, fresh_db):
    root, meta, pid, conn = _linked_project(tmp_path, fresh_db)
    before = _links(conn)
    assert all(pk is not None for pk, _ in before.values())
    mf = os.path.join(meta, f"{pid}_mapfile.csv")
    os.rename(mf, mf + ".bak")
    oingest.ingest_tree(conn, root, meta)
    assert _links(conn) == before
    row = conn.execute("SELECT metadata_status, metadata_file FROM projects").fetchone()
    assert row["metadata_status"] == "missing_mapfile"
    assert row["metadata_file"] == f"{pid}_mapfile.csv"


def test_reingest_broken_header_keeps_sample_links(tmp_path, fresh_db):
    root, meta, pid, conn = _linked_project(tmp_path, fresh_db)
    before = _links(conn)
    _write_mapfile(meta, pid, [], header="id;r1;r2")
    oingest.ingest_tree(conn, root, meta)
    assert _links(conn) == before
    # a new file found while the mapfile is broken is cataloged unlinked
    write_gz(os.path.join(root, pid, "new_R1.fastq.gz"))
    oingest.ingest_tree(conn, root, meta)
    links = _links(conn)
    assert links["new_R1.fastq.gz"] == (None, "R1")
    assert links["s1_fwd.fastq.gz"] == before["s1_fwd.fastq.gz"]


def test_moved_file_keeps_row_and_history(tmp_path, fresh_db):
    root, meta = _roots(tmp_path)
    pid = "genohub-1_X"
    for fn in ("s1_R1.fastq.gz", "s1_R2.fastq.gz"):
        write_gz(os.path.join(root, pid, "run1", fn))
    _write_mapfile(meta, pid, ["s1,s1_R1.fastq.gz,s1_R2.fastq.gz,Gadus,U1"])
    conn = fresh_db
    oingest.ingest_tree(conn, root, meta)
    conn.execute("UPDATE files SET md5_match=1")
    conn.commit()
    pks = {r["filename"]: r["file_pk"] for r in conn.execute("SELECT * FROM files")}

    os.makedirs(os.path.join(root, pid, "run2"))
    for fn in pks:
        os.rename(os.path.join(root, pid, "run1", fn), os.path.join(root, pid, "run2", fn))
    res = oingest.ingest_tree(conn, root, meta, prune=True)

    rows = conn.execute("SELECT * FROM files").fetchall()
    assert len(rows) == 2
    for r in rows:
        assert r["file_pk"] == pks[r["filename"]]
        assert r["rel_path"] == f"{pid}/run2/{r['filename']}"
        assert r["md5_match"] == 1
    assert res[0][3]["pruned_files"] == 0


def test_prune_deletes_by_rel_path_and_counts_rows(tmp_path, fresh_db):
    # Same basename in two lanes plus junk; only the paths the mapfile resolves to
    # survive a prune, and every deleted row is counted.
    root, meta = _roots(tmp_path)
    pid = "genohub-1_X"
    for sub in ("L1", "L2"):
        for fn in ("s1_R1.fastq.gz", "s1_R2.fastq.gz", "junk_R1.fastq.gz"):
            write_gz(os.path.join(root, pid, sub, fn))
    conn = fresh_db
    oingest.ingest_tree(conn, root, meta)  # diskonly: 6 rows
    _write_mapfile(meta, pid, ["s1,s1_R1.fastq.gz,s1_R2.fastq.gz,Gadus,U1"])
    res = oingest.ingest_tree(conn, root, meta, prune=True)
    rp = {r["rel_path"] for r in conn.execute("SELECT rel_path FROM files")}
    assert rp == {f"{pid}/L2/s1_R1.fastq.gz", f"{pid}/L2/s1_R2.fastq.gz"}
    assert res[0][3]["pruned_files"] == 4


def test_glob_metachar_project_name(tmp_path, fresh_db):
    from seqledger import validate as oval
    root, meta = _roots(tmp_path)
    pid = "LAB-run[1]_X"
    for fn in ("s1_R1.fastq.gz", "s1_R2.fastq.gz"):
        write_gz(os.path.join(root, pid, "run1", fn))
    _write_mapfile(meta, pid, ["s1,s1_R1.fastq.gz,s1_R2.fastq.gz,Gadus,U1"])
    conn = fresh_db
    oingest.ingest_tree(conn, root, meta)
    rows = conn.execute("SELECT rel_path, size_bytes FROM files").fetchall()
    assert {r["rel_path"] for r in rows} == {
        f"{pid}/run1/s1_R1.fastq.gz", f"{pid}/run1/s1_R2.fastq.gz"}
    assert all(r["size_bytes"] for r in rows)
    assert oval.validate_catalog(conn, root)[pid]["data"]["status"] == "ok"


def test_extra_cells_row_warns_and_flags(tmp_path, fresh_db):
    root, meta = _roots(tmp_path)
    pid = "genohub-1_X"
    for fn in ("s1_R1.fastq.gz", "s1_R2.fastq.gz"):
        write_gz(os.path.join(root, pid, fn))
    _write_mapfile(meta, pid,
                   ["s1,s1_R1.fastq.gz,s1_R2.fastq.gz,Gadus morhua, juvenile,USNM 123"])
    conn = fresh_db
    res = oingest.ingest_tree(conn, root, meta)
    findings = [f.message for f in res[0][1]]
    assert any("row 2: 1 extra cell(s)" in m for m in findings), findings
    assert "extra_cells" in conn.execute("SELECT flags FROM samples").fetchone()["flags"]
    assert res[0][3]["metadata_status"] == "flagged"


def test_prune_refuses_to_drop_most_samples_unless_forced(tmp_path, fresh_db):
    root, meta = _roots(tmp_path)
    pid = "genohub-1_X"
    lines = []
    for s in ("s1", "s2", "s3"):
        for d in ("R1", "R2"):
            write_gz(os.path.join(root, pid, f"{s}_{d}.fastq.gz"))
        lines.append(f"{s},{s}_R1.fastq.gz,{s}_R2.fastq.gz,Gadus,U{s}")
    _write_mapfile(meta, pid, lines)
    conn = fresh_db
    oingest.ingest_tree(conn, root, meta)

    _write_mapfile(meta, pid, lines[:1])  # truncated mapfile
    res = oingest.ingest_tree(conn, root, meta, prune=True)
    stats = res[0][3]
    assert stats["pruned_samples"] == [] and stats["pruned_files"] == 0
    assert "refusing to prune 2 of 3 samples" in stats["prune_reason"]
    assert any("refusing to prune" in f.message for f in res[0][1])
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 6

    res = oingest.ingest_tree(conn, root, meta, prune=True, force=True)
    assert res[0][3]["pruned_samples"] == ["s2", "s3"]
    assert res[0][3]["pruned_files"] == 4
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1


def test_prune_dry_run_lists_without_deleting(tmp_path, fresh_db):
    seq, meta = _roots(tmp_path)
    for pid in ("genohub-1_A", "genohub-2_B"):
        make_project(seq, pid, f"{pid}_mapfile.csv",
                     [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "U1")])
    conn = fresh_db
    oingest.ingest_tree(conn, seq, meta)
    __import__("shutil").rmtree(os.path.join(seq, "genohub-2_B"))
    os.remove(os.path.join(meta, "genohub-2_B_mapfile.csv"))
    res = oingest.prune_missing_projects(conn, seq, meta, dry_run=True)
    assert res["pruned"] == ["genohub-2_B"] and res["skipped"] is False
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 2


def test_mapfile_subpath_disambiguates_same_basename(tmp_path, fresh_db):
    # Same basename in Run1 and Run2; mapfile names each with its subfolder.
    from seqledger import validate as ov
    seq, meta = _roots(tmp_path)
    pid = "genohub-1_X"
    rows = [("s1", "Run1/x_R1.fastq.gz", "Run1/x_R2.fastq.gz", "Gadus", "U1"),
            ("s2", "Run2/x_R1.fastq.gz", "Run2/x_R2.fastq.gz", "Gadus", "U2")]
    make_project(seq, pid, f"{pid}_mapfile.csv", rows)
    conn = fresh_db
    results = oingest.ingest_tree(conn, seq, meta)
    assert results[0][2] == "pass", [str(f) for f in results[0][1]]
    rows_db = conn.execute(
        "SELECT filename, rel_path, size_bytes FROM files ORDER BY rel_path").fetchall()
    assert [(r["filename"], r["rel_path"]) for r in rows_db] == [
        ("x_R1.fastq.gz", f"{pid}/Run1/x_R1.fastq.gz"),
        ("x_R2.fastq.gz", f"{pid}/Run1/x_R2.fastq.gz"),
        ("x_R1.fastq.gz", f"{pid}/Run2/x_R1.fastq.gz"),
        ("x_R2.fastq.gz", f"{pid}/Run2/x_R2.fastq.gz")]
    assert all(r["size_bytes"] > 0 for r in rows_db)
    assert ov.check_data_files(conn, pid, pid, seq)["status"] == "ok"

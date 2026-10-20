"""rclone copy-job builder for the GUI custom-table export."""

import re

from seqledger import rclone as orclone


def _rows():
    return [
        {"seqdata_root": "/store/root", "rel_path": "projA/s1_1.fastq.gz", "size_bytes": 100},
        {"seqdata_root": "/store/root", "rel_path": "projA/s1_2.fastq.gz", "size_bytes": 200},
        {"seqdata_root": "/store/other", "rel_path": "projB/x_1.fastq.gz", "size_bytes": 50},
        {"seqdata_root": None, "rel_path": "unknown.fastq.gz", "size_bytes": None},
    ]


def test_human_size():
    assert orclone.human_size(0) == "0.00 GB"
    assert orclone.human_size(1610612736) == "1.50 GB"  # 1.5 * 1024**3
    assert orclone.human_size(None) == "unknown"


def test_estimate_size_sums_known_and_counts_unknown():
    total, n, n_unknown = orclone.estimate_size(_rows())
    assert total == 350
    assert n == 4
    assert n_unknown == 1


def test_group_by_root_groups_sorts_and_reports_unresolved():
    groups, n_unresolved = orclone.group_by_root(_rows())
    assert n_unresolved == 1  # the None-root row
    assert groups == [
        ("/store/other", ["projB/x_1.fastq.gz"]),
        ("/store/root", ["projA/s1_1.fastq.gz", "projA/s1_2.fastq.gz"]),
    ]


def test_build_copy_script_has_ltio_directives_and_rclone():
    groups, _ = orclone.group_by_root(_rows())
    script = orclone.build_copy_script(
        groups, "/pool/dest", transfers=4, slots=4, mem=4,
        est_bytes=350, n_files=3)
    # lTIO queue + resource directives
    assert "#$ -q lTIO.sq -l ioq" in script
    assert "#$ -pe mthread 4" in script
    assert "#$ -l mres=16G,h_data=4G,h_vmem=4G" in script  # slots*mem = 16
    # rclone module + copy with a files-from list per source root
    assert "module load tools/rclone/1.66.0" in script
    assert script.count("rclone copy") == 2  # one per source root
    assert "--files-from" in script and "--transfers 4" in script
    assert "projA/s1_1.fastq.gz" in script and "projB/x_1.fastq.gz" in script
    assert "DEST=/pool/dest" in script  # shell-quoted destination (no quoting needed)
    assert "estimated transfer: 0.00 GB across 3 file(s)" in script
    # guard: refuse to run without a usable destination
    assert 'if [ -z "$DEST" ]' in script
    assert "no destination set" in script
    assert "not a writable directory" in script
    assert "--progress" not in script


def test_build_copy_script_stamped_log_and_failure_exit():
    groups, _ = orclone.group_by_root(_rows())
    script = orclone.build_copy_script(groups, "/pool/dest", stamp="20261015_120000")
    assert "#$ -o seqledger_rclone_copy_20261015_120000.log" in script
    assert "rc_fail=0" in script
    assert script.count("rc_fail=$rc") == 2  # one per rclone copy
    assert script.rstrip().endswith("exit $rc_fail")
    # default stamp is YYYYmmdd_HHMMSS
    assert re.search(r"-o seqledger_rclone_copy_\d{8}_\d{6}\.log",
                     orclone.build_copy_script(groups, "/pool/dest"))


def test_head_groups_caps_total_files():
    groups = [("/a", ["1", "2", "3"]), ("/b", ["4", "5"]), ("/c", ["6"])]
    assert orclone.head_groups(groups, 4) == [("/a", ["1", "2", "3"]), ("/b", ["4"])]
    assert orclone.head_groups(groups, 20) == groups


def test_build_copy_script_empty_selection():
    script = orclone.build_copy_script([], "/pool/dest")
    assert "no files selected" in script
    assert "DEST=/pool/dest" in script  # dest was provided, not the placeholder


def test_build_copy_script_no_destination_uses_placeholder():
    # When no dest is given, DEST is the placeholder and the guard exits.
    script = orclone.build_copy_script([("/store/x", ["p/a.fastq.gz"])], "")
    assert "DEST=REPLACE_WITH_DESTINATION" in script
    assert 'if [ -z "$DEST" ]' in script


# --- md5 listing scripts for unverified backups ---

def _projects():
    return [("genohub-1_A", "/store/root"), ("genohub-2_B", "/store/other")]


def test_build_md5_store_script_one_rclone_per_project_then_cat():
    script = orclone.build_md5_store_script(_projects(), mem=2)
    assert script.startswith("#!/bin/bash")
    assert "#$ -q lTIO.sq -l ioq" in script
    assert "module load tools/rclone/1.66.0" in script
    assert script.count("rclone md5sum") == 2
    assert 'rclone md5sum /store/root --include genohub-1_A/\'**\'' in script \
        or "rclone md5sum /store/root --include 'genohub-1_A/**'" in script
    assert "store_genohub-2_B.md5" in script
    # a finished per-project listing is skipped on resubmit; partial output never kept
    assert '[ -s store_genohub-1_A.md5 ]' in script
    assert ".tmp" in script
    assert "cat store_*.md5 > store.md5" in script


def test_build_md5_store_script_rejects_unsafe_project_id():
    import pytest
    with pytest.raises(ValueError):
        orclone.build_md5_store_script([("bad id;rm", "/store/root")])


def test_build_md5_pdrive_bat_uses_root_and_rclone_exe():
    bat = orclone.build_md5_pdrive_bat(["genohub-1_A", "genohub-2_B"],
                                       r"P:\Hydra_backup\store\raw_sequence_data",
                                       rclone_exe=r"C:\tools\rclone.exe")
    assert bat.startswith("@echo off")
    assert "\r\n" in bat
    assert 'set "ROOT=P:\\Hydra_backup\\store\\raw_sequence_data"' in bat
    assert 'set "RCLONE=C:\\tools\\rclone.exe"' in bat
    assert bat.count('"%RCLONE%" md5sum "%ROOT%" --include') == 2
    assert '--include "genohub-1_A/**" > "pdrive_genohub-1_A.md5.tmp"' in bat
    assert 'if exist "pdrive_genohub-1_A.md5"' in bat
    assert "type pdrive_*.md5 > pdrive.md5" in bat


def test_build_md5_pdrive_bat_placeholder_when_root_unset():
    bat = orclone.build_md5_pdrive_bat(["genohub-1_A"], "")
    assert "REPLACE_WITH_PDRIVE_ROOT" in bat
    assert 'set "RCLONE=rclone"' in bat


def test_md5_projects_default_unverified_only(tmp_path):
    from seqledger import db as odb
    conn = odb.connect(str(tmp_path / "cat.db"))
    odb.init_db(conn)
    for pid in ("genohub-1_A", "genohub-2_B", "genohub-3_C"):
        conn.execute("INSERT INTO projects (project_id, seqdata_root) VALUES (?, ?)",
                     (pid, "/store/root"))
    conn.execute("INSERT INTO backups (project_id, location, verified, n_files, n_mismatch) "
                 "VALUES ('genohub-1_A', 'pdrive', 1, 4, 0)")
    conn.execute("INSERT INTO backups (project_id, location, verified, n_files, n_mismatch) "
                 "VALUES ('genohub-2_B', 'pdrive', 0, 4, 1)")
    conn.commit()
    rows = orclone.md5_projects(conn)
    assert [(r["project_id"], r["verified"]) for r in rows] == \
        [("genohub-2_B", 0), ("genohub-3_C", 0)]
    rows = orclone.md5_projects(conn, all_projects=True)
    assert [(r["project_id"], r["verified"], r["seqdata_root"]) for r in rows] == \
        [("genohub-1_A", 1, "/store/root"), ("genohub-2_B", 0, "/store/root"),
         ("genohub-3_C", 0, "/store/root")]

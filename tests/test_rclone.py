"""rclone copy-job builder for the GUI custom-table export."""

from seqledger import rclone as orclone


def _rows():
    return [
        {"seqdata_root": "/store/root", "rel_path": "projA/s1_1.fastq.gz", "size_bytes": 100},
        {"seqdata_root": "/store/root", "rel_path": "projA/s1_2.fastq.gz", "size_bytes": 200},
        {"seqdata_root": "/store/other", "rel_path": "projB/x_1.fastq.gz", "size_bytes": 50},
        {"seqdata_root": None, "rel_path": "unknown.fastq.gz", "size_bytes": None},
    ]


def test_human_size():
    assert orclone.human_size(0) == "0 B"
    assert orclone.human_size(1536) == "1.5 KB"
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
    assert "estimated transfer: 350 B across 3 file(s)" in script
    # guard: refuse to run without a usable destination
    assert 'if [ -z "$DEST" ]' in script
    assert "no destination set" in script
    assert "not a writable directory" in script


def test_build_copy_script_empty_selection():
    script = orclone.build_copy_script([], "/pool/dest")
    assert "no files selected" in script
    assert "DEST=/pool/dest" in script  # dest was provided, not the placeholder


def test_build_copy_script_no_destination_uses_placeholder():
    # When no dest is given, DEST is the placeholder and the guard exits.
    script = orclone.build_copy_script([("/store/x", ["p/a.fastq.gz"])], "")
    assert "DEST=REPLACE_WITH_DESTINATION" in script
    assert 'if [ -z "$DEST" ]' in script

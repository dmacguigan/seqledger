from odna.validate import validate_metadata, FAIL, WARN

HEADER = ["ID", "R1", "R2", "Taxon", "UniqID"]


def _rows(*tuples):
    return [dict(zip(HEADER, t)) for t in tuples]


def test_valid_project_passes():
    rows = _rows(("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus", "USNM 1"))
    findings, has_fail = validate_metadata(
        "proj_mapfile.csv", HEADER, rows,
        disk_filenames={"s1_1.fastq.gz", "s1_2.fastq.gz"})
    assert not has_fail
    assert findings == []


def test_bad_suffix_and_header_fail():
    findings, has_fail = validate_metadata("proj.csv", ["a", "b"], [])
    assert has_fail
    assert any("_mapfile.csv" in f.message for f in findings)


def test_uniqueid_alias_accepted():
    header = ["ID", "R1", "R2", "Taxon", "UniqueID"]
    rows = [dict(zip(header, ("s1", "a.fastq.gz", "b.fastq.gz", "Gadus", "X")))]
    _, has_fail = validate_metadata("proj_mapfile.csv", header, rows)
    assert not has_fail


def test_empty_field_and_identical_r1r2_and_dup_id_fail():
    rows = _rows(
        ("s1", "s1_1.fastq.gz", "s1_1.fastq.gz", "Gadus", "U1"),  # R1==R2
        ("s2", "s2_1.fastq.gz", "s2_2.fastq.gz", "", "U2"),        # empty Taxon
        ("s1", "s3_1.fastq.gz", "s3_2.fastq.gz", "Gadus", "U3"),   # dup ID
    )
    findings, has_fail = validate_metadata("proj_mapfile.csv", HEADER, rows)
    assert has_fail
    msgs = " ".join(f.message for f in findings)
    assert "identical" in msgs
    assert "empty required" in msgs
    assert "duplicate sample ID" in msgs


def test_substring_orphan_detected_exactly():
    # Regression: old substring test would treat "1.fastq.gz" as present because it
    # is a substring of the metadata entry "s_1.fastq.gz". Exact matching flags it.
    rows = _rows(("s1", "s_1.fastq.gz", "s_2.fastq.gz", "Gadus", "U1"))
    disk = {"s_1.fastq.gz", "s_2.fastq.gz", "1.fastq.gz"}
    findings, has_fail = validate_metadata("proj_mapfile.csv", HEADER, rows, disk_filenames=disk)
    assert not has_fail
    orphans = [f for f in findings if f.level == WARN and "1.fastq.gz" in f.message
               and "not referenced" in f.message]
    assert len(orphans) == 1


def test_cross_project_uniqid_warns():
    rows = _rows(("s1", "a.fastq.gz", "b.fastq.gz", "Gadus", "SHARED"))
    findings, _ = validate_metadata(
        "proj_mapfile.csv", HEADER, rows, known_uniq_ids={"SHARED": "other_proj"})
    assert any(f.level == WARN and "already cataloged" in f.message for f in findings)

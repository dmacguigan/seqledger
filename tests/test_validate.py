from seqledger.validate import validate_metadata, FAIL, WARN

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


def test_content_problems_warn_not_fail():
    # Row content problems no longer FAIL the project -- they load with NA-fill /
    # skip and surface as WARN findings (see plan_rows).
    rows = _rows(
        ("s1", "s1_1.fastq.gz", "s1_1.fastq.gz", "Gadus", "U1"),  # R1==R2
        ("s2", "s2_1.fastq.gz", "s2_2.fastq.gz", "", "U2"),        # empty Taxon -> NA
        ("s1", "s3_1.fastq.gz", "s3_2.fastq.gz", "Gadus", "U3"),   # dup ID -> skipped
    )
    findings, has_fail = validate_metadata("proj_mapfile.csv", HEADER, rows)
    assert not has_fail
    msgs = " ".join(f.message for f in findings)
    assert "same file" in msgs                # R1==R2 flag
    assert "Taxon was empty" in msgs          # NA-fill
    assert "skipped (duplicate sample ID" in msgs


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


def test_plan_rows_na_fill_skip_and_read_flags():
    from seqledger.validate import plan_rows
    rows = _rows(
        ("clean", "a_1.fastq.gz", "a_2.fastq.gz", "Gadus", "U1"),   # clean
        ("noTax", "b_1.fastq.gz", "b_2.fastq.gz", "", "U2"),         # na_taxon
        ("noR2",  "c_1.fastq.gz", "", "Gadus", "U3"),                # missing_r2
        ("same",  "d.fastq.gz", "d.fastq.gz", "Gadus", "U4"),        # r1_eq_r2
        ("",      "e_1.fastq.gz", "e_2.fastq.gz", "Gadus", "U5"),    # empty ID -> skip
        ("clean", "f_1.fastq.gz", "f_2.fastq.gz", "Gadus", "U6"),    # dup ID -> skip
    )
    uniqid_col, plans = plan_rows(HEADER, rows)
    assert uniqid_col == "UniqID"
    by = {p.get("sample_id"): p for p in plans if p["load"]}
    assert by["clean"]["flags"] == []
    assert by["noTax"]["taxon"] == "NA" and by["noTax"]["flags"] == ["na_taxon"]
    assert "missing_r2" in by["noR2"]["flags"]
    assert "r1_eq_r2" in by["same"]["flags"]
    skips = [p["skip_reason"] for p in plans if not p["load"]]
    assert any("empty ID" in s for s in skips)
    assert any("duplicate sample ID" in s for s in skips)

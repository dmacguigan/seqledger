"""MitoPilot map-file builder for the GUI Grab & Go export."""

from seqledger import mitopilot as omito


def _samples():
    return [
        {"project_id": "P1", "sample_id": "s1", "taxon": "Gadus morhua", "uniq_id": "U1"},
        {"project_id": "P1", "sample_id": "s2", "taxon": "Urophycis sp.", "uniq_id": "U2"},
    ]


def _files():
    return [
        {"project_id": "P1", "sample_id": "s1", "direction": "R1", "filename": "s1_1.fastq.gz"},
        {"project_id": "P1", "sample_id": "s1", "direction": "R2", "filename": "s1_2.fastq.gz"},
        {"project_id": "P1", "sample_id": "s2", "direction": "R1", "filename": "s2_1.fastq.gz"},
        {"project_id": "P1", "sample_id": "s2", "direction": "R2", "filename": "s2_2.fastq.gz"},
    ]


def test_build_map_rows_default_id_is_sample_id():
    rows = omito.build_map_rows(_samples(), _files())
    assert rows[0] == {"ID": "s1", "R1": "s1_1.fastq.gz",
                       "R2": "s1_2.fastq.gz", "Taxon": "Gadus morhua"}
    assert [r["ID"] for r in rows] == ["s1", "s2"]


def test_build_map_rows_uses_chosen_id_field():
    rows = omito.build_map_rows(_samples(), _files(), id_field="uniq_id")
    assert [r["ID"] for r in rows] == ["U1", "U2"]


def test_build_map_rows_missing_reads_are_blank():
    # sample with no file rows -> empty R1/R2 (MitoPilot needs both)
    samples = [{"project_id": "P1", "sample_id": "s9", "taxon": "X", "uniq_id": "U9"}]
    rows = omito.build_map_rows(samples, [])
    assert rows[0]["R1"] == "" and rows[0]["R2"] == ""


def test_build_map_rows_handles_nan_from_pandas_records():
    nan = float("nan")
    samples = [{"project_id": "P1", "sample_id": "s1", "taxon": nan, "uniq_id": nan}]
    files = [{"project_id": "P1", "sample_id": "s1", "direction": "R1", "filename": "a.gz"},
             {"project_id": "P1", "sample_id": "s1", "direction": nan, "filename": "b.gz"}]
    rows = omito.build_map_rows(samples, files, id_field="uniq_id")
    assert rows[0]["ID"] == "" and rows[0]["Taxon"] == ""
    assert rows[0]["R1"] == "a.gz" and rows[0]["R2"] == ""  # NaN direction ignored


def test_issues_flags_empty_dupes_and_missing_reads():
    rows = [
        {"ID": "x", "R1": "a", "R2": "b", "Taxon": "T"},
        {"ID": "x", "R1": "c", "R2": "d", "Taxon": "T"},   # duplicate ID
        {"ID": "",  "R1": "e", "R2": "f", "Taxon": "T"},   # empty ID
        {"ID": "y", "R1": "g", "R2": "",  "Taxon": "T"},   # missing R2
    ]
    iss = omito.issues(rows)
    assert iss["n_empty_id"] == 1
    assert iss["duplicate_ids"] == ["x"]
    assert iss["n_missing_reads"] == 1

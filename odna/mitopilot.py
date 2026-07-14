"""Build a MitoPilot sample-mapping CSV for a set of selected samples.

MitoPilot (https://github.com/Smithsonian/MitoPilot) takes a CSV with four
required columns -- ID, R1, R2, Taxon -- where R1/R2 are the raw Illumina
paired-end *filenames* only (the data directory is given separately to MitoPilot
via its data_path). The ID column is chosen by the user (e.g. the sample_id or
the UniqID), so this builder takes an id_field naming which sample column to use.
"""

MITOPILOT_COLUMNS = ["ID", "R1", "R2", "Taxon"]


def _s(v):
    """Coerce a cell to a clean string; None / NaN -> '' (pandas records use NaN)."""
    if v is None or v != v:
        return ""
    return str(v).strip()


def build_map_rows(sample_rows, file_rows, id_field="sample_id"):
    """Return MitoPilot map rows [{ID, R1, R2, Taxon}, ...] for the given samples.

    sample_rows: mappings with 'project_id', 'sample_id', 'taxon', and id_field.
    file_rows:   mappings with 'project_id', 'sample_id', 'direction', 'filename'.
    R1/R2 are the file basenames. Rows come out in sample_rows order; a sample
    with no R1/R2 on file gets empty strings for those (MitoPilot needs both).
    """
    by_sample = {}
    for f in file_rows:
        key = (f["project_id"], f["sample_id"])
        direction = _s(f.get("direction"))
        if direction:
            by_sample.setdefault(key, {})[direction] = _s(f.get("filename"))
    rows = []
    for s in sample_rows:
        key = (s["project_id"], s["sample_id"])
        d = by_sample.get(key, {})
        rows.append({"ID": _s(s.get(id_field)), "R1": d.get("R1", ""),
                     "R2": d.get("R2", ""), "Taxon": _s(s.get("taxon"))})
    return rows


def issues(map_rows):
    """Flag problems that would break a MitoPilot run: duplicate/empty IDs, missing reads.

    Returns dict: n_empty_id, duplicate_ids (sorted list), n_missing_reads.
    """
    seen = {}
    empty = missing = 0
    for r in map_rows:
        if not r["ID"]:
            empty += 1
        else:
            seen[r["ID"]] = seen.get(r["ID"], 0) + 1
        if not r["R1"] or not r["R2"]:
            missing += 1
    return {"n_empty_id": empty,
            "duplicate_ids": sorted(i for i, c in seen.items() if c > 1),
            "n_missing_reads": missing}

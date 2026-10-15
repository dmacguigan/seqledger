"""Build a MitoPilot sample-mapping CSV for a set of selected samples.

MitoPilot (https://github.com/Smithsonian/MitoPilot) takes a CSV with four
required columns -- ID, R1, R2, Taxon. MitoPilot joins its data_path and R1/R2
verbatim, so R1/R2 here are paths relative to the copy destination (or to the
Store seqdata root), i.e. each file's rel_path; give MitoPilot DEST as data_path.
MitoPilot takes one file per read direction: a lane-split sample (several R1 or
R2 files) gets its first file in sorted order and is flagged by issues(). The ID
column is chosen by the user (e.g. the sample_id or the UniqID), so this builder
takes an id_field naming which sample column to use.
"""

MITOPILOT_COLUMNS = ["ID", "R1", "R2", "Taxon"]


def _s(v):
    """Coerce a cell to a clean string; None / NaN -> '' (pandas records use NaN)."""
    if v is None or v != v:
        return ""
    return str(v).strip()


def build_map_rows(sample_rows, file_rows, id_field="sample_id"):
    """Return MitoPilot map rows [{ID, R1, R2, Taxon, lane_split}, ...].

    sample_rows: mappings with 'project_id', 'sample_id', 'taxon', and id_field.
    file_rows:   mappings with 'project_id', 'sample_id', 'direction', and
                 'rel_path' (falls back to 'filename' when rel_path is absent).
    R1/R2 are the first file per direction in sorted order. Rows come out in
    sample_rows order; a sample with no R1/R2 on file gets empty strings for those
    (MitoPilot needs both). 'lane_split' (not a MitoPilot column) is the sample_id
    when the sample has several files in either direction, else ''.
    """
    by_sample = {}
    for f in file_rows:
        key = (f["project_id"], f["sample_id"])
        direction = _s(f.get("direction"))
        path = _s(f.get("rel_path")) or _s(f.get("filename"))
        if direction and path:
            by_sample.setdefault(key, {}).setdefault(direction, []).append(path)
    rows = []
    for s in sample_rows:
        key = (s["project_id"], s["sample_id"])
        d = {k: sorted(v) for k, v in by_sample.get(key, {}).items()}
        split = any(len(v) > 1 for v in d.values())
        rows.append({"ID": _s(s.get(id_field)), "R1": d.get("R1", [""])[0],
                     "R2": d.get("R2", [""])[0], "Taxon": _s(s.get("taxon")),
                     "lane_split": _s(s["sample_id"]) if split else ""})
    return rows


def issues(map_rows):
    """Flag problems that would break a MitoPilot run: duplicate/empty IDs, missing
    reads, lane-split samples (only their first file per direction is mapped).

    Returns dict: n_empty_id, duplicate_ids (sorted list), n_missing_reads,
    n_lane_split, lane_split_ids (sorted list of sample IDs).
    """
    seen = {}
    empty = missing = 0
    lane = []
    for r in map_rows:
        if not r["ID"]:
            empty += 1
        else:
            seen[r["ID"]] = seen.get(r["ID"], 0) + 1
        if not r["R1"] or not r["R2"]:
            missing += 1
        if r.get("lane_split"):
            lane.append(r["lane_split"])
    return {"n_empty_id": empty,
            "duplicate_ids": sorted(i for i, c in seen.items() if c > 1),
            "n_missing_reads": missing,
            "n_lane_split": len(lane),
            "lane_split_ids": sorted(lane)}

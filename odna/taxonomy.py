"""Pure-Python NCBI taxonomy resolution for the Ocean DNA catalog.

Ports RiboPilot's approach (R/taxonomy.R): a pinned local NCBI taxdump, exact
name->taxid, then genus-anchored fuzzy matching, plus a ranked lineage. The
taxdump is parsed once into a SQLite index so per-taxon lookups are fast and
low-memory. Stdlib only, no external binaries.
"""

import csv
import os
import re
import sqlite3
import tarfile
import urllib.request
from datetime import date, datetime

TAXDUMP_URL = "https://ftp.ncbi.nlm.nih.gov/pub/taxonomy/taxdump.tar.gz"

# Target ranks captured into per-rank columns. NCBI 'superkingdom' -> domain.
RANKS = ["domain", "kingdom", "phylum", "class", "order", "family", "genus", "species"]
RANK_COLUMNS = ["tax_" + r for r in RANKS]
_RANK_ALIASES = {"superkingdom": "domain"}

_OPEN_NOMEN = re.compile(r"\b(sp|spp|cf|aff|nr|indet)\.?\b.*$", re.IGNORECASE)
_PARENS = re.compile(r"\([^)]*\)")
_WS = re.compile(r"\s+")
_TOKEN_OK = re.compile(r"^[A-Za-z.-]+$")


def clean_taxon(x):
    """Normalize a free-text Taxon to a queryable 'Genus species' (port of R)."""
    if not x:
        return ""
    x = x.strip().replace("_", " ")
    x = _WS.sub(" ", x)
    x = _OPEN_NOMEN.sub("", x)
    x = _PARENS.sub("", x)
    x = _WS.sub(" ", x).strip()
    toks = [t for t in x.split(" ") if _TOKEN_OK.match(t)]
    return " ".join(toks[:2])


# ---- taxdump download + SQLite index ----------------------------------------

def _index_path(taxdir):
    return os.path.join(taxdir, "taxdump.sqlite")


def ensure_taxdump(taxdir, force=False):
    """Download + extract names.dmp / nodes.dmp into taxdir (once)."""
    os.makedirs(taxdir, exist_ok=True)
    names = os.path.join(taxdir, "names.dmp")
    nodes = os.path.join(taxdir, "nodes.dmp")
    if os.path.exists(names) and os.path.exists(nodes) and not force:
        return taxdir
    tgz = os.path.join(taxdir, "taxdump.tar.gz")
    print(f"Downloading NCBI taxdump (~72 MB) to {taxdir} ...")
    urllib.request.urlretrieve(TAXDUMP_URL, tgz)
    with tarfile.open(tgz) as tf:
        for member in ("names.dmp", "nodes.dmp"):
            tf.extract(member, taxdir)
    with open(os.path.join(taxdir, "TAXDUMP_VERSION.txt"), "w") as f:
        f.write(f"source: {TAXDUMP_URL}\n")
        f.write(f"downloaded: {datetime.now().isoformat(timespec='seconds')}\n")
    return taxdir


def _iter_dmp(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            yield [c.strip() for c in line.rstrip("\t|\n").split("\t|\t")]


def build_index(taxdir, force=False):
    """Parse names.dmp + nodes.dmp into a SQLite index (idempotent)."""
    idx_path = _index_path(taxdir)
    if os.path.exists(idx_path) and not force:
        return idx_path
    ensure_taxdump(taxdir)
    tmp = idx_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.executescript(
            "CREATE TABLE tax_nodes(taxid INTEGER PRIMARY KEY, parent INTEGER, rank TEXT);"
            "CREATE TABLE tax_names(name_lower TEXT, name TEXT, taxid INTEGER, name_class TEXT);")
        con.executemany(
            "INSERT INTO tax_nodes VALUES (?,?,?)",
            ((int(p[0]), int(p[1]), p[2]) for p in _iter_dmp(os.path.join(taxdir, "nodes.dmp"))))
        con.executemany(
            "INSERT INTO tax_names VALUES (?,?,?,?)",
            ((p[1].lower(), p[1], int(p[0]), p[3])
             for p in _iter_dmp(os.path.join(taxdir, "names.dmp"))))
        con.executescript(
            "CREATE INDEX idx_names_lower ON tax_names(name_lower);"
            "CREATE INDEX idx_names_taxid ON tax_names(taxid);"
            "CREATE INDEX idx_nodes_parent ON tax_nodes(parent);")
        con.commit()
    finally:
        con.close()
    os.replace(tmp, idx_path)
    return idx_path


def open_index(taxdir):
    return sqlite3.connect(_index_path(taxdir))


# ---- index-backed lookups ---------------------------------------------------

def name_to_taxid(idx, name):
    """Case-insensitive name -> taxid, preferring the scientific name."""
    rows = idx.execute(
        "SELECT taxid, name_class FROM tax_names WHERE name_lower=?", (name.lower(),)).fetchall()
    if not rows:
        return None
    for taxid, cls in rows:
        if cls == "scientific name":
            return taxid
    return rows[0][0]


def _sci_name(idx, taxid):
    row = idx.execute(
        "SELECT name FROM tax_names WHERE taxid=? AND name_class='scientific name' LIMIT 1",
        (taxid,)).fetchone()
    return row[0] if row else None


def ranked_lineage(idx, taxid):
    """Walk parents to root; return (sci_name, finest_rank, {tax_<rank>: name}).

    The parent chain is walked via the tax_nodes PK (indexed), then every
    scientific name for the chain is fetched in a single batched query -- this
    avoids an N+1 _sci_name lookup per ancestor for each resolved taxon.
    """
    ranks = {c: None for c in RANK_COLUMNS}
    chain = []  # [(taxid, rank)] from the input node up to (but not incl.) root
    cur = taxid
    seen = set()
    while cur and cur not in seen and cur != 1:
        seen.add(cur)
        row = idx.execute("SELECT parent, rank FROM tax_nodes WHERE taxid=?", (cur,)).fetchone()
        if row is None:
            break
        parent, rank = row
        chain.append((cur, rank))
        cur = parent
    if not chain:
        return None, None, ranks
    ids = [c[0] for c in chain]
    names = dict(idx.execute(
        "SELECT taxid, name FROM tax_names "
        "WHERE name_class='scientific name' AND taxid IN (%s)"
        % ",".join("?" * len(ids)), ids).fetchall())
    for tid, rank in chain:
        col_rank = _RANK_ALIASES.get(rank, rank)
        if col_rank in RANKS:
            ranks["tax_" + col_rank] = names.get(tid)
    sci_name, finest_rank = names.get(chain[0][0]), chain[0][1]
    return sci_name, finest_rank, ranks


def genus_species(idx, genus_taxid):
    """Species-rank descendants of a genus (taxid, scientific name)."""
    return idx.execute(
        """WITH RECURSIVE sub(taxid) AS (
             SELECT taxid FROM tax_nodes WHERE parent=?
             UNION ALL
             SELECT n.taxid FROM tax_nodes n JOIN sub ON n.parent=sub.taxid)
           SELECT n.taxid, nm.name FROM tax_nodes n
             JOIN sub ON n.taxid=sub.taxid
             JOIN tax_names nm ON nm.taxid=n.taxid AND nm.name_class='scientific name'
           WHERE n.rank='species'""", (genus_taxid,)).fetchall()


def _levenshtein(a, b):
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ---- resolution -------------------------------------------------------------

def _blank(taxon, clean):
    d = {"taxon": taxon, "clean": clean, "match_type": "unresolved",
         "taxid": None, "sci_name": None, "rank": None,
         "lineage": None, "alternatives": None}
    for c in RANK_COLUMNS:
        d[c] = None
    return d


def _fill_lineage(idx, d, taxid):
    sci_name, rank, ranks = ranked_lineage(idx, taxid)
    d["taxid"] = taxid
    d["sci_name"] = sci_name
    d["rank"] = rank
    d.update(ranks)
    parts = [ranks["tax_" + r] for r in RANKS if ranks["tax_" + r]]
    d["lineage"] = "; ".join(parts) if parts else None


def _resolve_one(idx, taxon):
    clean = clean_taxon(taxon)
    d = _blank(taxon, clean)
    if not clean:
        return d
    tx = name_to_taxid(idx, clean)
    if tx is not None:
        d["match_type"] = "exact"
        _fill_lineage(idx, d, tx)
        return d
    toks = clean.split(" ")
    if len(toks) == 2:
        genus, epithet = toks
        gtx = name_to_taxid(idx, genus)
        if gtx is not None:
            sp = genus_species(idx, gtx)
            if sp:
                scored = sorted(sp, key=lambda r: _levenshtein(clean.lower(), r[1].lower()))
                best_taxid, best_name = scored[0]
                dist = _levenshtein(clean.lower(), best_name.lower())
                if dist <= max(2, -(-len(epithet) // 3)):
                    d["match_type"] = "fuzzy_species"
                    d["alternatives"] = " | ".join(f"{n} [{i}]" for i, n in scored[:5])
                    _fill_lineage(idx, d, best_taxid)
                    return d
            d["match_type"] = "fuzzy_genus"
            _fill_lineage(idx, d, gtx)
            return d
    return d


def _dedup(taxa):
    seen, out = set(), []
    for t in taxa:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def resolve_taxa(taxa, taxdir):
    """Resolve a list of raw Taxon strings (deduped). Returns list of dicts."""
    build_index(taxdir)
    idx = open_index(taxdir)
    try:
        return [_resolve_one(idx, t) for t in _dedup(taxa)]
    finally:
        idx.close()


# ---- catalog integration ----------------------------------------------------

_TAXA_COLS = (["taxon", "clean", "match_type", "taxid", "sci_name", "rank"]
              + RANK_COLUMNS + ["lineage", "alternatives"])


def _upsert_taxon(conn, d, today):
    cols = _TAXA_COLS + ["date_resolved"]
    vals = [d.get(c) for c in _TAXA_COLS] + [today]
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols)
    conn.execute(
        f"INSERT INTO taxa ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(taxon) DO UPDATE SET {updates}", vals)


def resolve_catalog(conn, taxdir, redo=False):
    """Resolve distinct sample taxa and upsert into the `taxa` table.

    By default only taxa not yet confirmed are (re-)resolved; redo=True also
    re-resolves confirmed ones. Returns the list of resolution dicts.
    """
    if redo:
        rows = conn.execute(
            "SELECT DISTINCT taxon FROM samples WHERE taxon IS NOT NULL AND taxon!=''")
    else:
        rows = conn.execute(
            """SELECT DISTINCT s.taxon FROM samples s
               LEFT JOIN taxa t ON t.taxon = s.taxon
               WHERE s.taxon IS NOT NULL AND s.taxon != ''
                 AND (t.taxon IS NULL OR COALESCE(t.confirmed, 0) = 0)""")
    taxa = [r[0] for r in rows]
    results = resolve_taxa(taxa, taxdir)
    today = date.today().isoformat()
    for d in results:
        _upsert_taxon(conn, d, today)
    conn.commit()
    return results


def write_review_csv(results, path):
    """Write a review CSV; user edits confirmed_taxid then runs apply."""
    fields = ["taxon", "clean", "match_type", "taxid", "sci_name", "rank",
              "lineage", "alternatives", "confirmed_taxid"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for d in results:
            tx = "" if d["taxid"] is None else d["taxid"]
            w.writerow({
                "taxon": d["taxon"], "clean": d["clean"], "match_type": d["match_type"],
                "taxid": tx, "sci_name": d["sci_name"] or "", "rank": d["rank"] or "",
                "lineage": d["lineage"] or "", "alternatives": d["alternatives"] or "",
                "confirmed_taxid": tx})


def _apply_confirmed(conn, d, today):
    cols = _TAXA_COLS[1:] + ["date_resolved"]
    updates = ",".join(f"{c}=?" for c in cols) + ", confirmed=1"
    vals = [d.get(c) for c in _TAXA_COLS[1:]] + [today, d["taxon"]]
    conn.execute(f"UPDATE taxa SET {updates} WHERE taxon=?", vals)


def apply_review(conn, taxdir, csv_path):
    """Fold user-confirmed taxids from a review CSV back into `taxa`."""
    build_index(taxdir)
    idx = open_index(taxdir)
    today = date.today().isoformat()
    n = 0
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ct = (row.get("confirmed_taxid") or "").strip()
                if not ct:
                    continue
                d = _blank(row["taxon"], clean_taxon(row["taxon"]))
                _fill_lineage(idx, d, int(ct))
                d["match_type"] = "confirmed"
                _apply_confirmed(conn, d, today)
                n += 1
        conn.commit()
    finally:
        idx.close()
    return n

"""WoRMS (World Register of Marine Species) taxonomy resolution.

A companion to `taxonomy.py` (NCBI): resolves the same free-text `samples.taxon`
strings against the authoritative marine register, storing an AphiaID + accepted
name + ranked lineage in the `worms_*` columns of the `taxa` table -- side by side
with the NCBI columns, keyed on the same raw `taxon`.

Unlike the NCBI taxdump (a pinned local file), WoRMS has no free flat-file dump, so
this queries the WoRMS REST API (stdlib urllib + json, no dependencies, no API key)
using its batch TAXAMATCH fuzzy matcher. Responses are cached in a local SQLite so
re-runs are cheap and repeat/offline runs still work. WoRMS's top rank is Kingdom
(no domain), and it flags synonyms (status / valid_AphiaID) -- a matched synonym is
followed to its accepted record so the stored lineage/name is the accepted one.
"""

import csv
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from seqledger.taxonomy import clean_taxon

WORMS_REST = "https://www.marinespecies.org/rest"
WORMS_TAXON_URL = "https://www.marinespecies.org/aphia.php?p=taxdetails&id="

# WoRMS's highest rank is Kingdom -- there is no domain/superkingdom, so the WoRMS
# lineage is one rank shorter than the NCBI one.
WORMS_RANKS = ["kingdom", "phylum", "class", "order", "family", "genus", "species"]
WORMS_RANK_COLUMNS = ["worms_" + r for r in WORMS_RANKS]

_BATCH = 50            # names per AphiaRecordsByMatchNames call (GET URL length safe)
_TIMEOUT = 60          # seconds per request
_RETRY_CODES = (429, 500, 502, 503, 504)
_MISS = object()       # cache sentinel: key absent (vs. a cached empty/None value)


# ---- HTTP + cache -----------------------------------------------------------

def _fetch_json(url):
    """GET url and return parsed JSON (dict/list), or None for a 204/no-content.

    Isolated so tests can monkeypatch it with canned AphiaRecord JSON (no network).
    Retries transient 429/5xx a few times with a short backoff.
    """
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                if getattr(resp, "status", 200) == 204:
                    return None
                body = resp.read()
            return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            if e.code == 204:
                return None
            if e.code in _RETRY_CODES and attempt < 2:
                last = e
                time.sleep(1 + attempt)
                continue
            raise
        except urllib.error.URLError as e:
            if attempt < 2:
                last = e
                time.sleep(1 + attempt)
                continue
            raise
    if last:
        raise last
    return None


def _cache_path(taxdir):
    return os.path.join(taxdir, "worms_cache.sqlite")


def _cache_open(taxdir):
    """Open (creating if needed) the local WoRMS response cache."""
    os.makedirs(taxdir, exist_ok=True)
    con = sqlite3.connect(_cache_path(taxdir))
    con.execute("CREATE TABLE IF NOT EXISTS worms_cache("
                "query TEXT PRIMARY KEY, response_json TEXT, fetched TEXT)")
    return con


def _cache_get(cache, key):
    row = cache.execute(
        "SELECT response_json FROM worms_cache WHERE query=?", (key,)).fetchone()
    if row is None:
        return _MISS
    return json.loads(row[0])


def _cache_put(cache, key, value):
    cache.execute(
        "INSERT OR REPLACE INTO worms_cache(query, response_json, fetched) VALUES (?,?,?)",
        (key, json.dumps(value), date.today().isoformat()))


# ---- REST lookups (cache-backed) --------------------------------------------

def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _dedup(items):
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _fetch_match_chunk(names, marine_only):
    """One AphiaRecordsByMatchNames call: {name: [AphiaRecord, ...]} for the chunk."""
    params = [("scientificnames[]", n) for n in names]
    params.append(("marine_only", "true" if marine_only else "false"))
    url = f"{WORMS_REST}/AphiaRecordsByMatchNames?" + urllib.parse.urlencode(params)
    data = _fetch_json(url)  # None, or a list aligned to the input order
    out = {}
    for i, name in enumerate(names):
        recs = data[i] if data and i < len(data) else None
        out[name] = recs or []
    return out


def match_names(names, cache, marine_only=False, progress=False):
    """Batch-match cleaned names via TAXAMATCH; returns {name: [AphiaRecord, ...]}.

    Cache-backed per name, so only never-seen names hit the API. `marine_only=False`
    keeps brackish/freshwater samples (WoRMS covers them) rather than dropping them.
    The network batches are the slow part, so progress is reported here (per batch).
    """
    names = _dedup(names)
    out, misses = {}, []
    for n in names:
        cached = _cache_get(cache, n)
        if cached is not _MISS:
            out[n] = cached
        else:
            misses.append(n)
    if progress and misses:
        cached_n = len(names) - len(misses)
        print(f"  querying WoRMS for {len(misses)} name(s)"
              + (f" ({cached_n} cached)" if cached_n else ""))
    done = 0
    for chunk in _chunks(misses, _BATCH):
        got = _fetch_match_chunk(chunk, marine_only)
        for n in chunk:
            out[n] = got.get(n, [])
            _cache_put(cache, n, out[n])
        cache.commit()
        done += len(chunk)
        if progress:
            print(f"\r  matched {done}/{len(misses)} name(s)", end="", flush=True)
    if progress and misses:
        print()
    return out


def _record_by_aphia_id(aphia_id, cache):
    """AphiaRecord for an AphiaID (accepted-name lookup), cache-backed."""
    key = f"aphia:{aphia_id}"
    cached = _cache_get(cache, key)
    if cached is not _MISS:
        return cached or None
    rec = _fetch_json(f"{WORMS_REST}/AphiaRecordByAphiaID/{aphia_id}")
    _cache_put(cache, key, rec)
    cache.commit()
    return rec


# ---- record -> taxa dict ----------------------------------------------------

def _blank(taxon, clean):
    d = {"taxon": taxon, "clean": clean,
         "aphia_id": None, "worms_sci_name": None, "worms_status": None,
         "worms_match_type": "unresolved", "worms_rank": None,
         "worms_lineage": None, "worms_alternatives": None, "worms_is_marine": None}
    for c in WORMS_RANK_COLUMNS:
        d[c] = None
    return d


def _fill_fields(d, rec):
    """Populate the worms_* rank/name/lineage columns from one AphiaRecord."""
    d["aphia_id"] = rec.get("AphiaID")
    d["worms_sci_name"] = rec.get("valid_name") or rec.get("scientificname")
    d["worms_rank"] = rec.get("rank")
    for r in WORMS_RANKS[:-1]:  # kingdom..genus are inline on the record
        d["worms_" + r] = rec.get(r)
    if (rec.get("rank") or "").lower() == "species":
        d["worms_species"] = rec.get("valid_name") or rec.get("scientificname")
    im = rec.get("isMarine")
    d["worms_is_marine"] = None if im is None else int(bool(im))
    parts = [d["worms_" + r] for r in WORMS_RANKS if d["worms_" + r]]
    d["worms_lineage"] = "; ".join(parts) if parts else None


def _resolve_one(taxon, records, cache):
    """Build the worms_* dict for one raw taxon from its candidate records.

    Takes the best (first) candidate; if it is a synonym (status != accepted with a
    valid_AphiaID) the accepted record is fetched so the stored lineage/name is the
    accepted one, while the original status is retained.
    """
    d = _blank(taxon, clean_taxon(taxon))
    if not records:
        return d
    matched = records[0]
    match_type = (matched.get("match_type") or "exact").lower()
    status = matched.get("status")
    rec = matched
    if status and status.lower() != "accepted" and matched.get("valid_AphiaID"):
        acc = _record_by_aphia_id(matched["valid_AphiaID"], cache)
        if acc:
            rec = acc
    _fill_fields(d, rec)
    d["worms_status"] = status
    d["worms_match_type"] = match_type
    alts = _dedup([r.get("scientificname") for r in records[:5]])
    d["worms_alternatives"] = " | ".join(alts) if alts else None
    return d


def resolve_taxa_worms(taxa, taxdir, progress=True, marine_only=False):
    """Resolve a list of raw Taxon strings against WoRMS (deduped). List of dicts.

    Many raw strings clean to the same query name, so the API is queried once per
    distinct cleaned name and the result mapped back to each raw taxon.
    """
    taxa = _dedup(taxa)
    cleans = {t: clean_taxon(t) for t in taxa}
    cache = _cache_open(taxdir)
    out = []
    try:
        matches = match_names([c for c in cleans.values() if c], cache, marine_only,
                              progress=progress)
        # Mapping records -> rows is cheap, except the occasional synonym-follow fetch;
        # the counter reflects it every 100 taxa (and at the end) like the NCBI path.
        total = len(taxa)
        for i, t in enumerate(taxa, 1):
            out.append(_resolve_one(t, matches.get(cleans[t], []), cache))
            if progress and (i % 100 == 0 or i == total):
                print(f"\r  resolved {i}/{total} taxa", end="", flush=True)
        if progress and total:
            print()
    finally:
        cache.close()
    return out


# ---- catalog integration ----------------------------------------------------

# worms_* columns written by an automated resolve (excludes worms_confirmed, which
# is user-owned, and worms_date_resolved, appended at write time).
_WORMS_DB_COLS = (["aphia_id", "worms_sci_name", "worms_status", "worms_match_type",
                   "worms_rank"] + WORMS_RANK_COLUMNS
                  + ["worms_lineage", "worms_alternatives", "worms_is_marine"])


def _upsert_worms(conn, d, today):
    cols = ["taxon"] + _WORMS_DB_COLS + ["worms_date_resolved"]
    vals = [d["taxon"]] + [d.get(c) for c in _WORMS_DB_COLS] + [today]
    placeholders = ",".join("?" for _ in cols)
    upd = ",".join(f"{c}=excluded.{c}" for c in _WORMS_DB_COLS + ["worms_date_resolved"])
    # Never let an automated re-resolve overwrite a user-confirmed WoRMS row (same
    # guard as the NCBI side). The taxa row may not exist yet if NCBI never ran, so
    # this INSERTs it (NCBI columns left NULL) and updates only the worms_* columns.
    conn.execute(
        f"INSERT INTO taxa ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(taxon) DO UPDATE SET {upd} "
        f"WHERE COALESCE(taxa.worms_confirmed, 0) = 0",
        vals)


def resolve_catalog_worms(conn, taxdir, scope="new", marine_only=False):
    """Resolve distinct sample taxa against WoRMS and upsert the worms_* columns.

    scope mirrors the NCBI resolver but on WoRMS state:
      "new"          taxa with no WoRMS resolution yet (worms_date_resolved IS NULL).
      "unconfirmed"  also re-resolve WoRMS rows resolved before but not confirmed.
      "all"          re-resolve every distinct taxon, including confirmed ones.
    """
    if scope == "all":
        rows = conn.execute(
            "SELECT DISTINCT taxon FROM samples WHERE taxon IS NOT NULL AND taxon!=''")
    elif scope == "unconfirmed":
        rows = conn.execute(
            """SELECT DISTINCT s.taxon FROM samples s
               LEFT JOIN taxa t ON t.taxon = s.taxon
               WHERE s.taxon IS NOT NULL AND s.taxon != ''
                 AND (t.taxon IS NULL OR COALESCE(t.worms_confirmed, 0) = 0)""")
    else:  # "new": no WoRMS resolution recorded yet
        rows = conn.execute(
            """SELECT DISTINCT s.taxon FROM samples s
               LEFT JOIN taxa t ON t.taxon = s.taxon
               WHERE s.taxon IS NOT NULL AND s.taxon != ''
                 AND (t.taxon IS NULL OR t.worms_date_resolved IS NULL)""")
    taxa = [r[0] for r in rows]
    results = resolve_taxa_worms(taxa, taxdir, marine_only=marine_only)
    today = date.today().isoformat()
    for d in results:
        _upsert_worms(conn, d, today)
    conn.commit()
    return results


# ---- review CSV + apply -----------------------------------------------------

def write_review_csv_worms(results, path):
    """Write a WoRMS review CSV; user edits confirmed_aphia_id then runs apply."""
    fields = ["taxon", "clean", "worms_match_type", "aphia_id", "worms_sci_name",
              "worms_status", "worms_rank", "worms_lineage", "worms_alternatives",
              "confirmed_aphia_id"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for d in results:
            aid = "" if d["aphia_id"] is None else d["aphia_id"]
            w.writerow({
                "taxon": d["taxon"], "clean": d["clean"],
                "worms_match_type": d["worms_match_type"], "aphia_id": aid,
                "worms_sci_name": d["worms_sci_name"] or "",
                "worms_status": d["worms_status"] or "",
                "worms_rank": d["worms_rank"] or "",
                "worms_lineage": d["worms_lineage"] or "",
                "worms_alternatives": d["worms_alternatives"] or "",
                "confirmed_aphia_id": aid})


def _apply_confirmed_worms(conn, d, today):
    cols = _WORMS_DB_COLS + ["worms_date_resolved"]
    sets = ",".join(f"{c}=?" for c in cols) + ", worms_confirmed=1"
    vals = [d.get(c) for c in _WORMS_DB_COLS] + [today, d["taxon"]]
    # The taxa row may not exist if NCBI never resolved this taxon; ensure it does.
    conn.execute("INSERT OR IGNORE INTO taxa (taxon) VALUES (?)", (d["taxon"],))
    conn.execute(f"UPDATE taxa SET {sets} WHERE taxon=?", vals)


def apply_review_worms(conn, taxdir, csv_path):
    """Fold user-confirmed AphiaIDs from a WoRMS review CSV back into `taxa`.

    Each row is validated on its own (like the NCBI apply): a non-numeric or
    unknown confirmed_aphia_id is reported and skipped, not fatal. Returns
    (applied, skipped) where skipped is a list of "taxon: reason" strings.
    """
    cache = _cache_open(taxdir)
    today = date.today().isoformat()
    applied, skipped = 0, []
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                taxon = (row.get("taxon") or "").strip()
                ca = (row.get("confirmed_aphia_id") or "").strip()
                if not ca:
                    continue
                if not taxon:
                    skipped.append(f"(blank taxon): row has confirmed_aphia_id '{ca}'")
                    continue
                try:
                    aphia_id = int(ca)
                except ValueError:
                    skipped.append(f"{taxon}: confirmed_aphia_id '{ca}' is not a number")
                    continue
                rec = _record_by_aphia_id(aphia_id, cache)
                if not rec:
                    skipped.append(f"{taxon}: AphiaID {aphia_id} not found in WoRMS")
                    continue
                d = _blank(taxon, clean_taxon(taxon))
                _fill_fields(d, rec)
                d["worms_status"] = rec.get("status")
                d["worms_match_type"] = "confirmed"
                _apply_confirmed_worms(conn, d, today)
                applied += 1
        conn.commit()
    finally:
        cache.close()
    return applied, skipped

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

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from seqledger.taxonomy import (_apply_rows, _dedup, _review_reader, _taxa_in_scope,
                                 _write_review, _write_taxa, clean_taxon)

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

# WoRMS TAXAMATCH quality gate: an exact hit or a single-edit near-match is
# trustworthy enough to auto-accept as an AphiaID; phonetic / near_2+ matches are
# too loose to store as authoritative unreviewed, so they are flagged for review.
_ACCEPT_MATCH_TYPES = ("exact", "near_1")


def is_high_confidence(match_type):
    return (match_type or "").lower() in _ACCEPT_MATCH_TYPES


# ---- HTTP + cache -----------------------------------------------------------

def _fetch_json(url):
    """GET url and return parsed JSON (dict/list), or None for an empty body.

    Isolated so tests can monkeypatch it with canned AphiaRecord JSON (no network).
    Retries transient 429/5xx a few times with a short backoff.
    """
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                body = resp.read()
            return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            if e.code in _RETRY_CODES and attempt < 2:
                time.sleep(1 + attempt)
                continue
            raise
        except urllib.error.URLError:
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            raise


def _cache_open(taxdir):
    """Open (creating if needed) the local WoRMS response cache."""
    os.makedirs(taxdir, exist_ok=True)
    con = sqlite3.connect(os.path.join(taxdir, "worms_cache.sqlite"))
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
              + (f" ({cached_n} cached)" if cached_n else ""), file=sys.stderr)
    done = 0
    for chunk in _chunks(misses, _BATCH):
        got = _fetch_match_chunk(chunk, marine_only)
        for n in chunk:
            out[n] = got.get(n, [])
            _cache_put(cache, n, out[n])
        cache.commit()
        done += len(chunk)
        if progress and sys.stderr.isatty():
            print(f"\r  matched {done}/{len(misses)} name(s)", end="", file=sys.stderr,
                  flush=True)
    if progress and misses and sys.stderr.isatty():
        print(file=sys.stderr)
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
    return {**dict.fromkeys(_WORMS_DB_COLS), "taxon": taxon, "clean": clean,
            "worms_match_type": "unresolved"}


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
    # Record the candidate names regardless of quality so a reviewer can see them.
    alts = _dedup([r.get("scientificname") for r in records[:5]])
    d["worms_alternatives"] = " | ".join(alts) if alts else None
    # Quality gate: only a high-confidence match is written as an accepted AphiaID.
    # A loose phonetic / near_2+ match is left unwritten (aphia_id/lineage blank,
    # worms_confirmed stays 0) and flagged for review, since committing it as
    # authoritative would silently mis-assign the taxon.
    if not is_high_confidence(match_type):
        d["worms_match_type"] = ("review_" + match_type) if match_type else "review"
        return d
    status = matched.get("status")
    rec = matched
    if status and status.lower() != "accepted" and matched.get("valid_AphiaID"):
        acc = _record_by_aphia_id(matched["valid_AphiaID"], cache)
        if acc:
            rec = acc
    _fill_fields(d, rec)
    d["worms_status"] = status
    d["worms_match_type"] = match_type
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
        tty = progress and sys.stderr.isatty()
        for i, t in enumerate(taxa, 1):
            out.append(_resolve_one(t, matches.get(cleans[t], []), cache))
            if tty and (i % 100 == 0 or i == total):
                print(f"\r  resolved {i}/{total} taxa", end="", file=sys.stderr, flush=True)
        if tty and total:
            print(file=sys.stderr)
    finally:
        cache.close()
    return out


# ---- catalog integration ----------------------------------------------------

# worms_* columns written by an automated resolve (excludes worms_confirmed, which
# is user-owned, and worms_date_resolved, appended at write time).
_WORMS_DB_COLS = (["aphia_id", "worms_sci_name", "worms_status", "worms_match_type",
                   "worms_rank"] + WORMS_RANK_COLUMNS
                  + ["worms_lineage", "worms_alternatives", "worms_is_marine"])


def resolve_catalog_worms(conn, taxdir, scope="new", marine_only=False):
    """Resolve distinct sample taxa against WoRMS and upsert the worms_* columns.

    scope mirrors the NCBI resolver but on WoRMS state:
      "new"          taxa with no WoRMS resolution yet (worms_date_resolved IS NULL).
      "unconfirmed"  also re-resolve WoRMS rows resolved before but not confirmed.
      "all"          re-resolve every distinct taxon. Confirmed rows are kept:
                     their machine result is returned but not written.
    """
    results = resolve_taxa_worms(_taxa_in_scope(conn, scope, "worms_"), taxdir,
                                 marine_only=marine_only)
    _write_taxa(conn, results, _WORMS_DB_COLS, "worms_")
    return results


# ---- review CSV + apply -----------------------------------------------------

def write_review_csv_worms(results, path):
    """Write a WoRMS review CSV; user edits confirmed_aphia_id then runs apply."""
    _write_review(results, path, ["taxon", "clean", "worms_match_type", "aphia_id",
                                  "worms_sci_name", "worms_status", "worms_rank",
                                  "worms_lineage", "worms_alternatives"],
                  "aphia_id", "worms_match_type")


def apply_review_worms(conn, taxdir, csv_path):
    """Fold user-confirmed AphiaIDs from a WoRMS review CSV back into `taxa`.

    Each row is validated on its own (like the NCBI apply): a non-numeric or
    unknown confirmed_aphia_id is reported and skipped, not fatal. Returns
    (applied, skipped) where skipped is a list of "taxon: reason" strings.
    Raises ValueError if the CSV has no confirmed_aphia_id column.
    """
    reader = _review_reader(csv_path, "confirmed_aphia_id", "not a WoRMS review CSV "
                            "(an NCBI one needs --source ncbi)")
    cache = _cache_open(taxdir)

    def lookup(taxon, aphia_id):
        rec = _record_by_aphia_id(aphia_id, cache)
        if not rec:
            return f"AphiaID {aphia_id} not found in WoRMS"
        d = _blank(taxon, clean_taxon(taxon))
        _fill_fields(d, rec)
        d["worms_status"] = rec.get("status")
        d["worms_match_type"] = "confirmed"
        return d

    try:
        return _apply_rows(conn, reader, "confirmed_aphia_id", lookup, _WORMS_DB_COLS,
                           "worms_")
    finally:
        cache.close()

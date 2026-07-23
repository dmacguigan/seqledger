"""Tests for WoRMS resolution (seqledger/worms.py).

The WoRMS REST API is never hit: `worms._fetch_json` is monkeypatched with canned
AphiaRecord JSON so the suite is fully offline and deterministic.
"""

from urllib.parse import parse_qs, urlparse

from seqledger import db as odb
from seqledger import worms as oworms

# --- canned AphiaRecords -----------------------------------------------------

THUNNUS = {"AphiaID": 127029, "scientificname": "Thunnus thynnus", "rank": "Species",
           "status": "accepted", "valid_AphiaID": 127029, "valid_name": "Thunnus thynnus",
           "kingdom": "Animalia", "phylum": "Chordata", "class": "Teleostei",
           "order": "Scombriformes", "family": "Scombridae", "genus": "Thunnus",
           "isMarine": 1, "match_type": "exact"}

# A synonym record (unaccepted) pointing at its accepted name via valid_AphiaID.
SARDINOPS_SYN = {"AphiaID": 273208, "scientificname": "Sardinops caeruleus",
                 "rank": "Species", "status": "unaccepted", "valid_AphiaID": 158885,
                 "valid_name": "Sardinops sagax", "kingdom": "Animalia",
                 "phylum": "Chordata", "class": "Teleostei", "order": "Clupeiformes",
                 "family": "Clupeidae", "genus": "Sardinops", "isMarine": 1,
                 "match_type": "exact"}
SARDINOPS_ACCEPTED = {"AphiaID": 158885, "scientificname": "Sardinops sagax",
                      "rank": "Species", "status": "accepted", "valid_AphiaID": 158885,
                      "valid_name": "Sardinops sagax", "kingdom": "Animalia",
                      "phylum": "Chordata", "class": "Teleostei", "order": "Clupeiformes",
                      "family": "Clupeidae", "genus": "Sardinops", "isMarine": 1,
                      "match_type": "exact"}

# A fuzzy (near_1) hit for a misspelled genus.
GADUS = {"AphiaID": 125732, "scientificname": "Gadus", "rank": "Genus",
         "status": "accepted", "valid_AphiaID": 125732, "valid_name": "Gadus",
         "kingdom": "Animalia", "phylum": "Chordata", "class": "Teleostei",
         "order": "Gadiformes", "family": "Gadidae", "genus": "Gadus",
         "isMarine": 1, "match_type": "near_1"}

_BY_NAME = {
    "Thunnus thynnus": [THUNNUS],
    "Sardinops caeruleus": [SARDINOPS_SYN],
    "Gadus": [GADUS],
}
_BY_ID = {127029: THUNNUS, 158885: SARDINOPS_ACCEPTED, 125732: GADUS}


class _Fetcher:
    """Canned _fetch_json with a call counter (to assert the cache is used)."""

    def __init__(self):
        self.calls = 0

    def __call__(self, url):
        self.calls += 1
        if "AphiaRecordsByMatchNames" in url:
            names = parse_qs(urlparse(url).query).get("scientificnames[]", [])
            return [_BY_NAME.get(n, []) for n in names]
        if "AphiaRecordByAphiaID/" in url:
            aphia_id = int(url.rsplit("/", 1)[1])
            return _BY_ID.get(aphia_id)
        return None


def _patch(monkeypatch):
    f = _Fetcher()
    monkeypatch.setattr(oworms, "_fetch_json", f)
    return f


def _catalog(tmp_path, taxa):
    conn = odb.connect(str(tmp_path / "cat.db"))
    odb.init_db(conn)
    conn.execute("INSERT INTO projects(project_id) VALUES ('P1')")
    for i, tx in enumerate(taxa):
        conn.execute("INSERT INTO samples(project_id, sample_id, taxon) VALUES ('P1',?,?)",
                     (f"s{i}", tx))
    conn.commit()
    return conn


# --- resolution --------------------------------------------------------------

def test_migration_adds_worms_columns(tmp_path):
    conn = odb.connect(str(tmp_path / "c.db"))
    odb.init_db(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(taxa)")}
    for c in ["aphia_id", "worms_sci_name", "worms_status", "worms_match_type",
              "worms_kingdom", "worms_species", "worms_lineage", "worms_confirmed",
              "worms_date_resolved"]:
        assert c in cols
    assert "worms_domain" not in cols  # WoRMS has no domain rank


def test_exact_match(tmp_path, monkeypatch):
    _patch(monkeypatch)
    d = oworms.resolve_taxa_worms(["Thunnus thynnus"], str(tmp_path / "tax"))[0]
    assert d["worms_match_type"] == "exact"
    assert d["aphia_id"] == 127029
    assert d["worms_sci_name"] == "Thunnus thynnus"
    assert d["worms_status"] == "accepted"
    assert d["worms_family"] == "Scombridae"
    assert d["worms_species"] == "Thunnus thynnus"
    assert d["worms_is_marine"] == 1
    assert d["worms_lineage"] == (
        "Animalia; Chordata; Teleostei; Scombriformes; Scombridae; Thunnus; Thunnus thynnus")


def test_synonym_follows_to_accepted(tmp_path, monkeypatch):
    _patch(monkeypatch)
    d = oworms.resolve_taxa_worms(["Sardinops caeruleus"], str(tmp_path / "tax"))[0]
    # Stored lineage/name is the ACCEPTED taxon...
    assert d["aphia_id"] == 158885
    assert d["worms_sci_name"] == "Sardinops sagax"
    assert d["worms_family"] == "Clupeidae"
    # ...but the original match status is retained so synonyms are visible.
    assert d["worms_status"] == "unaccepted"


def test_fuzzy_match_type(tmp_path, monkeypatch):
    _patch(monkeypatch)
    # A misspelled genus cleans to "Gadis"? No -- clean_taxon keeps the raw token;
    # the API returns a near_1 candidate for whatever we send.
    d = oworms.resolve_taxa_worms(["Gadus"], str(tmp_path / "tax"))[0]
    assert d["worms_match_type"] == "near_1"
    assert d["worms_rank"] == "Genus"
    assert d["worms_genus"] == "Gadus"
    assert d["worms_species"] is None


def test_no_match_is_blank(tmp_path, monkeypatch):
    _patch(monkeypatch)
    d = oworms.resolve_taxa_worms(["Nonexistent blobfish"], str(tmp_path / "tax"))[0]
    assert d["aphia_id"] is None
    assert d["worms_match_type"] == "unresolved"
    assert d["worms_lineage"] is None


def test_cache_avoids_second_network_call(tmp_path, monkeypatch):
    f = _patch(monkeypatch)
    taxdir = str(tmp_path / "tax")
    oworms.resolve_taxa_worms(["Thunnus thynnus"], taxdir)
    after_first = f.calls
    assert after_first >= 1
    # Second run over the same name serves entirely from the on-disk cache.
    oworms.resolve_taxa_worms(["Thunnus thynnus"], taxdir)
    assert f.calls == after_first


# --- catalog integration -----------------------------------------------------

def test_resolve_catalog_and_scope_new(tmp_path, monkeypatch):
    _patch(monkeypatch)
    conn = _catalog(tmp_path, ["Thunnus thynnus", "Sardinops caeruleus"])
    taxdir = str(tmp_path / "tax")
    res = oworms.resolve_catalog_worms(conn, taxdir, scope="new")
    assert len(res) == 2
    row = conn.execute("SELECT aphia_id, worms_sci_name FROM taxa "
                       "WHERE taxon='Thunnus thynnus'").fetchone()
    assert row["aphia_id"] == 127029
    # scope='new' now finds nothing to do (all have worms_date_resolved).
    assert oworms.resolve_catalog_worms(conn, taxdir, scope="new") == []


def test_worms_upsert_leaves_ncbi_columns(tmp_path, monkeypatch):
    _patch(monkeypatch)
    conn = _catalog(tmp_path, ["Thunnus thynnus"])
    # Pretend NCBI already resolved this taxon.
    conn.execute("UPDATE taxa SET taxid=1, sci_name='x' WHERE 1=0")  # no-op guard
    conn.execute("INSERT INTO taxa(taxon, taxid, sci_name, match_type) "
                 "VALUES ('Thunnus thynnus', 999, 'NCBI name', 'exact')")
    conn.commit()
    oworms.resolve_catalog_worms(conn, str(tmp_path / "tax"), scope="new")
    row = conn.execute("SELECT taxid, sci_name, aphia_id FROM taxa "
                       "WHERE taxon='Thunnus thynnus'").fetchone()
    assert row["taxid"] == 999 and row["sci_name"] == "NCBI name"  # NCBI untouched
    assert row["aphia_id"] == 127029  # WoRMS filled in alongside


def test_apply_review_roundtrip_and_confirm_guard(tmp_path, monkeypatch):
    _patch(monkeypatch)
    conn = _catalog(tmp_path, ["Nonexistent blobfish"])
    taxdir = str(tmp_path / "tax")
    oworms.resolve_catalog_worms(conn, taxdir, scope="new")
    review = str(tmp_path / "worms_review.csv")
    results = oworms.resolve_taxa_worms(["Nonexistent blobfish"], taxdir)
    oworms.write_review_csv_worms(results, review)

    # User assigns the correct AphiaID by hand.
    import csv
    rows = list(csv.DictReader(open(review)))
    rows[0]["confirmed_aphia_id"] = "127029"
    with open(review, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)

    applied, skipped = oworms.apply_review_worms(conn, taxdir, review)
    assert applied == 1 and skipped == []
    row = conn.execute("SELECT aphia_id, worms_match_type, worms_confirmed FROM taxa "
                       "WHERE taxon='Nonexistent blobfish'").fetchone()
    assert row["aphia_id"] == 127029
    assert row["worms_match_type"] == "confirmed"
    assert row["worms_confirmed"] == 1

    # A confirmed WoRMS row must survive a full re-resolve.
    oworms.resolve_catalog_worms(conn, taxdir, scope="all")
    row = conn.execute("SELECT aphia_id, worms_confirmed FROM taxa "
                       "WHERE taxon='Nonexistent blobfish'").fetchone()
    assert row["aphia_id"] == 127029 and row["worms_confirmed"] == 1


def test_apply_review_skips_bad_aphia_id(tmp_path, monkeypatch):
    _patch(monkeypatch)
    conn = _catalog(tmp_path, ["Thunnus thynnus"])
    taxdir = str(tmp_path / "tax")
    review = str(tmp_path / "r.csv")
    with open(review, "w", newline="") as fh:
        fh.write("taxon,confirmed_aphia_id\n")
        fh.write("Thunnus thynnus,not-a-number\n")
        fh.write("Ghost taxon,99999999\n")   # numeric but unknown AphiaID
    applied, skipped = oworms.apply_review_worms(conn, taxdir, review)
    assert applied == 0
    assert len(skipped) == 2
    assert any("not a number" in s for s in skipped)
    assert any("not found in WoRMS" in s for s in skipped)


# --- #14: only high-confidence WoRMS matches are auto-accepted ----------------

# A loose (phonetic / near_2+) candidate that must NOT be written as authoritative.
BLOBFISH_PHONETIC = {"AphiaID": 555555, "scientificname": "Psychrolutes marcidus",
                     "rank": "Species", "status": "accepted", "valid_AphiaID": 555555,
                     "valid_name": "Psychrolutes marcidus", "kingdom": "Animalia",
                     "phylum": "Chordata", "class": "Teleostei", "order": "Perciformes",
                     "family": "Psychrolutidae", "genus": "Psychrolutes",
                     "isMarine": 1, "match_type": "phonetic"}


def test_is_high_confidence_pure():
    assert oworms.is_high_confidence("exact") is True
    assert oworms.is_high_confidence("near_1") is True
    assert oworms.is_high_confidence("EXACT") is True          # case-insensitive
    assert oworms.is_high_confidence("phonetic") is False
    assert oworms.is_high_confidence("near_2") is False
    assert oworms.is_high_confidence("near_3") is False
    assert oworms.is_high_confidence(None) is False
    assert oworms.is_high_confidence("") is False


def test_low_quality_match_flagged_not_accepted(tmp_path, monkeypatch):
    _patch(monkeypatch)
    # Inject the phonetic candidate under a query name (auto-restored after test).
    monkeypatch.setitem(_BY_NAME, "Blobbyfish", [BLOBFISH_PHONETIC])
    d = oworms.resolve_taxa_worms(["Blobbyfish"], str(tmp_path / "tax"))[0]
    # A phonetic match is NOT stored as an accepted AphiaID...
    assert d["aphia_id"] is None
    assert d["worms_lineage"] is None
    # ...it is flagged for review and its candidate names recorded as alternatives.
    assert d["worms_match_type"] == "review_phonetic"
    assert d["worms_alternatives"] == "Psychrolutes marcidus"


def test_high_quality_match_still_accepted(tmp_path, monkeypatch):
    _patch(monkeypatch)
    # near_1 remains high-confidence and is written as before.
    d = oworms.resolve_taxa_worms(["Gadus"], str(tmp_path / "tax"))[0]
    assert d["worms_match_type"] == "near_1"
    assert d["aphia_id"] == 125732

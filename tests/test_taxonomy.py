import os

from seqledger import db as odb
from seqledger import ingest as oingest
from seqledger import taxonomy as otax
from helpers import make_project, write_map_file

# Tiny synthetic taxdump:
# root -> Eukaryota -> Actinopterygii -> Gadidae -> {Gadus, Urophycis}
NODES = [
    (1, 1, "no rank"),
    (2759, 1, "superkingdom"),
    (7898, 2759, "class"),
    (8040, 7898, "family"),
    (8048, 8040, "genus"),
    (8049, 8048, "species"),
    (8050, 8048, "species"),
    (8060, 8040, "genus"),
    (8061, 8060, "species"),
]
NAMES = [
    (1, "root", "scientific name"),
    (2759, "Eukaryota", "scientific name"),
    (7898, "Actinopterygii", "scientific name"),
    (8040, "Gadidae", "scientific name"),
    (8048, "Gadus", "scientific name"),
    (8049, "Gadus morhua", "scientific name"),
    (8050, "Gadus macrocephalus", "scientific name"),
    (8060, "Urophycis", "scientific name"),
    (8061, "Urophycis chuss", "scientific name"),
]


def _write_taxdump(taxdir):
    os.makedirs(taxdir, exist_ok=True)
    with open(os.path.join(taxdir, "nodes.dmp"), "w") as f:
        for taxid, parent, rank in NODES:
            f.write(f"{taxid}\t|\t{parent}\t|\t{rank}\t|\t\t|\n")
    with open(os.path.join(taxdir, "names.dmp"), "w") as f:
        for taxid, name, cls in NAMES:
            f.write(f"{taxid}\t|\t{name}\t|\t\t|\t{cls}\t|\n")
    return taxdir


def _resolve(taxdir, taxon):
    return otax.resolve_taxa([taxon], taxdir)[0]


def test_clean_taxon():
    assert otax.clean_taxon("Gadus morhua") == "Gadus morhua"
    assert otax.clean_taxon("Urophycis sp.") == "Urophycis"
    assert otax.clean_taxon("Gadus cf. morhua") == "Gadus morhua"  # cf. keeps epithet
    assert otax.clean_taxon("Gadus_morhua (voucher 12)") == "Gadus morhua"
    assert otax.clean_taxon("Gadidae_Gadus_morhua") == "Gadus morhua"  # rank path
    assert otax.clean_taxon("B1116_#2") == ""  # pure junk / no name tokens


def test_build_index_idempotent(tmp_path):
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    p1 = otax.build_index(taxdir)
    assert os.path.exists(p1)
    p2 = otax.build_index(taxdir)  # no rebuild, no error
    assert p1 == p2


def test_exact_match(tmp_path):
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    d = _resolve(taxdir, "Gadus morhua")
    assert d["match_type"] == "exact"
    assert d["taxid"] == 8049
    assert d["rank"] == "species"


def test_fuzzy_species(tmp_path):
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    d = _resolve(taxdir, "Gadus morhuaa")  # one-char typo in epithet
    assert d["match_type"] == "fuzzy_species"
    assert d["taxid"] == 8049
    assert "Gadus morhua [8049]" in d["alternatives"]


def test_fuzzy_genus(tmp_path):
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    d = _resolve(taxdir, "Gadus zzzzzzz")  # genus known, epithet nowhere close
    assert d["match_type"] == "fuzzy_genus"
    assert d["taxid"] == 8048
    assert d["rank"] == "genus"


def test_unresolved(tmp_path):
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    d = _resolve(taxdir, "Zzz qqq")
    assert d["match_type"] == "unresolved"
    assert d["taxid"] is None


def test_family_genus_species(tmp_path):
    """Dominant catalog format Family_Genus_species resolves to the species."""
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    d = _resolve(taxdir, "Gadidae_Gadus_morhua")
    assert d["match_type"] == "exact"
    assert d["taxid"] == 8049 and d["rank"] == "species"


def test_family_genus_na_placeholder(tmp_path):
    """Family_Genus_NA drops the NA placeholder and resolves to the genus."""
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    d = _resolve(taxdir, "Gadidae_Gadus_NA")
    assert d["match_type"] == "exact"
    assert d["taxid"] == 8048 and d["rank"] == "genus"


def test_cf_keeps_epithet(tmp_path):
    """cf./aff. no longer discards the epithet after it."""
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    d = _resolve(taxdir, "Gadus cf. morhua")
    assert d["match_type"] == "exact" and d["taxid"] == 8049


def test_sp_nov_and_complex_markers(tmp_path):
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    assert _resolve(taxdir, "Urophycis n. sp.")["taxid"] == 8060       # -> genus
    assert _resolve(taxdir, "Gadus morhua Cmplx")["taxid"] == 8049     # -> species


def test_fuzzy_genus_correction(tmp_path):
    """A misspelled genus is corrected, then the epithet matched within it."""
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    d = _resolve(taxdir, "Gaduss morhua")  # genus typo (Gaduss -> Gadus)
    assert d["match_type"] == "fuzzy_species"
    assert d["taxid"] == 8049


def test_informal_lowercase_group(tmp_path):
    """A bare lowercase group name still matches (case-insensitive)."""
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    d = _resolve(taxdir, "gadidae")
    assert d["match_type"] == "exact" and d["taxid"] == 8040


def test_fuzzy_higher_rank(tmp_path):
    """A misspelled higher-rank name (family) is fuzzy-corrected."""
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    d = _resolve(taxdir, "Gaddidae")  # typo of family Gadidae
    assert d["match_type"] == "fuzzy_higher"
    assert d["taxid"] == 8040 and d["rank"] == "family"


def test_ranked_lineage_columns(tmp_path):
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    d = _resolve(taxdir, "Gadus morhua")
    assert d["tax_domain"] == "Eukaryota"
    assert d["tax_class"] == "Actinopterygii"
    assert d["tax_genus"] == "Gadus"
    assert d["tax_species"] == "Gadus morhua"
    assert d["tax_family"] == "Gadidae"
    assert d["tax_kingdom"] is None  # absent in this tree
    assert d["lineage"] == "Eukaryota; Actinopterygii; Gadidae; Gadus; Gadus morhua"


def test_resolve_catalog_and_apply(tmp_path):
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    root = str(tmp_path / "raw_sequence_data")
    os.makedirs(root, exist_ok=True)
    rows = [("s1", "s1_1.fastq.gz", "s1_2.fastq.gz", "Gadus morhua", "U1"),
            ("s2", "s2_1.fastq.gz", "s2_2.fastq.gz", "Zzz qqq", "U2")]
    make_project(root, "genohub-1_X", "genohub-1_X_mapfile.csv", rows)
    mf = write_map_file(root, [("genohub-1_X_mapfile.csv", "genohub-1_X")])
    conn = odb.connect(str(tmp_path / "cat.db"))
    odb.init_db(conn)
    oingest.ingest_map_file(conn, mf, seqdata_root=root)

    results = otax.resolve_catalog(conn, taxdir)
    assert len(results) == 2
    row = conn.execute(
        "SELECT taxid, match_type FROM taxa WHERE taxon='Gadus morhua'").fetchone()
    assert row["taxid"] == 8049 and row["match_type"] == "exact"
    url = otax.TAXDUMP_URL  # sanity: module constant present
    assert row["taxid"] and f"taxonomy/{row['taxid']}/" == f"taxonomy/8049/"

    # default scope="new" only resolves taxa with no row yet; both exist now -> none
    assert otax.resolve_catalog(conn, taxdir) == []

    # scope="unconfirmed" re-resolves unconfirmed taxa but skips confirmed ones
    conn.execute("UPDATE taxa SET confirmed=1 WHERE taxon='Zzz qqq'")
    conn.commit()
    again = otax.resolve_catalog(conn, taxdir, scope="unconfirmed")
    assert [d["taxon"] for d in again] == ["Gadus morhua"]

    # scope="all" re-resolves everything, including the confirmed taxon
    allres = otax.resolve_catalog(conn, taxdir, scope="all")
    assert sorted(d["taxon"] for d in allres) == ["Gadus morhua", "Zzz qqq"]


def test_apply_review_skips_bad_rows(tmp_path):
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    conn = odb.connect(os.path.join(tmp_path, "cat.db"))
    odb.init_db(conn)
    conn.execute("INSERT INTO taxa(taxon, match_type) VALUES ('Gadus morhua','unresolved')")
    conn.commit()
    csv_path = os.path.join(tmp_path, "review.csv")
    with open(csv_path, "w") as f:
        f.write("taxon,confirmed_taxid\n")
        f.write("Gadus morhua,8049\n")          # valid -> applied
        f.write("Bad taxon,notanumber\n")       # non-numeric -> skipped
        f.write("Ghost,99999999\n")             # not in taxdump -> skipped
    applied, skipped = otax.apply_review(conn, taxdir, csv_path)
    assert applied == 1
    assert len(skipped) == 2
    assert any("not a number" in m for m in skipped)
    assert any("not found" in m for m in skipped)
    row = conn.execute("SELECT confirmed, match_type FROM taxa WHERE taxon='Gadus morhua'").fetchone()
    assert row["confirmed"] == 1 and row["match_type"] == "confirmed"


# --- #20: apply must write (not silently no-op) when no taxa row exists yet ----

def test_apply_review_upserts_when_no_taxa_row(tmp_path):
    """A confirmed_taxid for a taxon with no prior `taxa` row must be persisted.

    Previously the bare UPDATE matched zero rows yet was still counted as applied,
    so the confirmation was reported but silently dropped.
    """
    taxdir = _write_taxdump(str(tmp_path / "tax"))
    conn = odb.connect(os.path.join(tmp_path, "cat.db"))
    odb.init_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM taxa").fetchone()[0] == 0  # no rows yet
    csv_path = os.path.join(tmp_path, "review.csv")
    with open(csv_path, "w") as f:
        f.write("taxon,confirmed_taxid\n")
        f.write("Gadus morhua,8049\n")
    applied, skipped = otax.apply_review(conn, taxdir, csv_path)
    assert applied == 1 and skipped == []
    row = conn.execute(
        "SELECT taxid, confirmed, match_type, sci_name FROM taxa "
        "WHERE taxon='Gadus morhua'").fetchone()
    assert row is not None                     # a row was actually written
    assert row["taxid"] == 8049
    assert row["confirmed"] == 1
    assert row["match_type"] == "confirmed"
    assert row["sci_name"] == "Gadus morhua"


# --- #11: cross-kingdom homonym must not be silently committed to one taxon ----

# Two genera share the exact scientific name 'Morus': a gannet (bird, Sulidae)
# and a mulberry (plant, Moraceae).
HOM_NODES = [
    (1, 1, "no rank"),
    (2759, 1, "superkingdom"),
    (33208, 2759, "kingdom"),      # Metazoa (animals)
    (9910, 33208, "family"),       # Sulidae
    (37698, 9910, "genus"),        # Morus (gannet)
    (33090, 2759, "kingdom"),      # Viridiplantae (plants)
    (3487, 33090, "family"),       # Moraceae
    (3497, 3487, "genus"),         # Morus (mulberry)
]
HOM_NAMES = [
    (1, "root", "scientific name"),
    (2759, "Eukaryota", "scientific name"),
    (33208, "Metazoa", "scientific name"),
    (9910, "Sulidae", "scientific name"),
    (37698, "Morus", "scientific name"),
    (33090, "Viridiplantae", "scientific name"),
    (3487, "Moraceae", "scientific name"),
    (3497, "Morus", "scientific name"),
]


def _write_dump(taxdir, nodes, names):
    os.makedirs(taxdir, exist_ok=True)
    with open(os.path.join(taxdir, "nodes.dmp"), "w") as f:
        for taxid, parent, rank in nodes:
            f.write(f"{taxid}\t|\t{parent}\t|\t{rank}\t|\t\t|\n")
    with open(os.path.join(taxdir, "names.dmp"), "w") as f:
        for taxid, name, cls in names:
            f.write(f"{taxid}\t|\t{name}\t|\t\t|\t{cls}\t|\n")
    return taxdir


def test_disambiguate_homonym_pure():
    # single candidate -> committed
    one = [(8049, "Gadus morhua", {"Gadus", "Gadidae"})]
    assert otax.disambiguate_homonym(one) == (8049, False, None)
    # context names exactly one -> that one is chosen
    two = [(37698, "Morus", {"Morus", "Sulidae", "Metazoa"}),
           (3497, "Morus", {"Morus", "Moraceae", "Viridiplantae"})]
    assert otax.disambiguate_homonym(two, context=["Sulidae"]) == (37698, False, None)
    # no context -> ambiguous, competing taxids reported, nothing committed
    tx, ambiguous, alts = otax.disambiguate_homonym(two)
    assert tx is None and ambiguous is True
    assert "Morus [37698]" in alts and "Morus [3497]" in alts
    # context that matches both is not a disambiguation -> still ambiguous
    _, ambiguous2, _ = otax.disambiguate_homonym(two, context=["Morus"])
    assert ambiguous2 is True


def test_homonym_bare_name_is_ambiguous(tmp_path):
    taxdir = _write_dump(str(tmp_path / "tax"), HOM_NODES, HOM_NAMES)
    d = _resolve(taxdir, "Morus")
    assert d["match_type"] == "ambiguous"
    assert d["taxid"] is None                  # no fabricated choice
    assert "Morus [37698]" in d["alternatives"]
    assert "Morus [3497]" in d["alternatives"]


def test_homonym_disambiguated_by_context(tmp_path):
    taxdir = _write_dump(str(tmp_path / "tax"), HOM_NODES, HOM_NAMES)
    d = _resolve(taxdir, "Sulidae Morus")       # family context -> the bird Morus
    assert d["match_type"] == "exact"
    assert d["taxid"] == 37698
    assert d["tax_family"] == "Sulidae"


# --- #12: a bare species epithet must not match a standalone name --------------

# Gadus tree plus a decoy genus 'Virginica' whose name collides with a common
# species epithet ('virginica').
EP_NODES = [
    (1, 1, "no rank"),
    (2759, 1, "superkingdom"),
    (7898, 2759, "class"),
    (8040, 7898, "family"),
    (8048, 8040, "genus"),          # Gadus
    (8049, 8048, "species"),        # Gadus morhua
    (5000, 7898, "genus"),          # Virginica (decoy genus, unrelated kingdom-mate)
]
EP_NAMES = [
    (1, "root", "scientific name"),
    (2759, "Eukaryota", "scientific name"),
    (7898, "Actinopterygii", "scientific name"),
    (8040, "Gadidae", "scientific name"),
    (8048, "Gadus", "scientific name"),
    (8049, "Gadus morhua", "scientific name"),
    (5000, "Virginica", "scientific name"),
]


def test_bare_epithet_pure():
    assert otax.bare_epithet(["Gadus", "morhua"]) == "morhua"
    assert otax.bare_epithet(["Genus", "Gadus", "morhua"]) == "morhua"  # trailing genus
    assert otax.bare_epithet(["gadidae"]) is None            # standalone lowercase name
    assert otax.bare_epithet(["Gadus"]) is None              # genus only, no epithet
    assert otax.bare_epithet(["Gadus", "Morhua"]) is None    # trailing cap, not an epithet


def test_bare_epithet_not_matched_when_genus_unknown(tmp_path):
    taxdir = _write_dump(str(tmp_path / "tax"), EP_NODES, EP_NAMES)
    # 'Nogenus' is absent; 'virginica' collides with the decoy genus Virginica.
    # The bare epithet must NOT resolve to that unrelated genus -> unresolved.
    d = _resolve(taxdir, "Nogenus virginica")
    assert d["match_type"] == "unresolved"
    assert d["taxid"] is None


def test_standalone_lowercase_group_still_resolves(tmp_path):
    """The guard only blocks epithets: a bare lowercase group name still matches."""
    taxdir = _write_dump(str(tmp_path / "tax"), EP_NODES, EP_NAMES)
    d = _resolve(taxdir, "gadidae")
    assert d["match_type"] == "exact" and d["taxid"] == 8040


# --- #19: taxdump download must be time-bounded --------------------------------

def test_download_passes_timeout(tmp_path, monkeypatch):
    import io
    captured = {}

    def fake_urlopen(url, timeout=None):
        captured["url"] = url
        captured["timeout"] = timeout
        return io.BytesIO(b"payload-bytes")

    monkeypatch.setattr(otax.urllib.request, "urlopen", fake_urlopen)
    dest = str(tmp_path / "out.bin")
    otax._download("http://example/taxdump.tar.gz", dest, timeout=42)
    assert captured["timeout"] == 42            # a timeout is passed to urlopen
    with open(dest, "rb") as f:
        assert f.read() == b"payload-bytes"     # response is streamed to disk


def test_download_default_timeout_is_bounded(tmp_path, monkeypatch):
    import io
    captured = {}

    def fake_urlopen(url, timeout=None):
        captured["timeout"] = timeout
        return io.BytesIO(b"x")

    monkeypatch.setattr(otax.urllib.request, "urlopen", fake_urlopen)
    otax._download("http://example/x", str(tmp_path / "o"))
    assert captured["timeout"] == otax._DOWNLOAD_TIMEOUT and captured["timeout"] > 0


def test_download_timeout_raises_clear_error(tmp_path, monkeypatch):
    import socket

    def fake_urlopen(url, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setattr(otax.urllib.request, "urlopen", fake_urlopen)
    try:
        otax._download("http://example/x", str(tmp_path / "o"), timeout=1)
    except RuntimeError as e:
        assert "Failed to download" in str(e)
    else:
        assert False, "expected a RuntimeError on timeout"

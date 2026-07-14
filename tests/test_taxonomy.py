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

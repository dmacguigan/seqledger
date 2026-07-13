#!/usr/bin/env python3
"""Ocean DNA catalog CLI.

Subcommands:
  init-db     create the catalog schema
  ingest      load CSV map files (metadata only) into the catalog
  checksums   load + compare `rclone md5sum` output from Store and P-drive
  validate    re-check the catalog and print findings
  integrity   gzip + FASTQ structural integrity check of cataloged files
  onboard     new batch: ingest + integrity + taxonomy resolve in one command
  taxonomy    resolve sample taxa to NCBI TaxIDs (resolve / apply)
  query       lookups (uniq-id, search, unbacked, mismatches, summary, taxa)
  gui         launch the Streamlit browse GUI (prints SSH tunnel command)
"""

import argparse
import os
import sys

from odna import db as odb
from odna import ingest as oingest
from odna import checksums as ochecksums
from odna import query as oquery
from odna import validate as ovalidate


def _print_rows(rows, cols):
    if not rows:
        print("(none)")
        return
    print("\t".join(cols))
    for r in rows:
        print("\t".join("" if r[c] is None else str(r[c]) for c in cols))


def cmd_init_db(args):
    conn = odb.connect(args.db)
    odb.init_db(conn)
    conn.close()
    print(f"initialized schema in {args.db}")


def cmd_ingest(args):
    conn = odb.connect(args.db)
    odb.init_db(conn)
    results = oingest.ingest_map_file(conn, args.map_file, seqdata_root=args.seqdata_root,
                                      metadata_root=args.metadata_root)
    conn.close()
    n_fail = 0
    for project_id, findings, status in results:
        icon = {"pass": "OK", "warn": "WARN", "fail": "FAIL"}[status]
        print(f"[{icon}] {project_id}")
        for f in findings:
            print(f"    {f.level}: {f.message}")
        if status == "fail":
            n_fail += 1
    print(f"\ningested {len(results)} project(s), {n_fail} rejected (FAIL)")


def cmd_checksums(args):
    conn = odb.connect(args.db)
    odb.init_db(conn)
    summary = ochecksums.load_checksums(
        conn, args.store, args.pdrive, source=args.source, only_project=args.project)
    conn.close()
    print(f"updated md5 for {summary['matched']} file(s) "
          f"across {len(summary['projects'])} project(s)")
    for w in summary["warnings"]:
        print(f"    WARN: {w}")


def _data_label(data):
    if data["status"] == "ok":
        return "OK"
    if data["status"] == "unchecked":
        return "unchecked"
    parts = []
    if data.get("n_missing"):
        parts.append(f"{data['n_missing']} missing")
    if data.get("n_orphan"):
        parts.append(f"{data['n_orphan']} orphan")
    return ", ".join(parts) or "issues"


def _checksum_label(cs):
    if cs["status"] == "verified":
        return "verified"
    if cs["status"] == "mismatch":
        return f"{cs['n_mismatch']} mismatch"
    if cs["status"] == "incomplete":
        return f"incomplete ({cs['n_uncompared']} uncompared)"
    return "no files"


def cmd_validate(args):
    conn = odb.connect(args.db)
    results = ovalidate.validate_catalog(conn, seqdata_root=args.seqdata_root)
    conn.close()
    if not results:
        print("catalog is empty")
        return
    if not args.seqdata_root:
        print("(data-files check skipped: pass --seqdata-root to scan disk)\n")
    for project_id in sorted(results):
        r = results[project_id]
        print(f"{project_id}"
              f"\n    data-files: {_data_label(r['data'])}"
              f"\n    checksum:   {_checksum_label(r['checksum'])}")
        for f in r["notes"]:
            print(f"    {f.level}: {f.message}")


def cmd_integrity(args):
    from odna import integrity as ointegrity
    conn = odb.connect(args.db)
    odb.init_db(conn)
    results = ointegrity.check_catalog_integrity(
        conn, seqdata_root=args.seqdata_root, only_project=args.project, jobs=args.jobs)
    conn.close()
    if not results:
        print("no cataloged files to check")
        return
    icons = {"pass": "OK", "warn": "WARN", "fail": "FAIL"}
    for project_id in sorted(results):
        s = results[project_id]
        print(f"[{icons[s['status']]}] {project_id}: {s['n_files']} file(s), "
              f"{s['n_ok']} ok, {s['n_gzip_error']} gzip-error, "
              f"{s['n_format_error']} format-error, {s['n_unchecked']} unchecked")
        for w in s["parity_warnings"]:
            print(f"    WARN: read-count parity: {w}")


def cmd_onboard(args):
    """Ingest + integrity + taxonomy resolve for a new batch, one map file."""
    from odna import integrity as ointegrity
    from odna import taxonomy as otax
    icons = {"pass": "OK", "warn": "WARN", "fail": "FAIL"}
    conn = odb.connect(args.db)
    odb.init_db(conn)

    print("== ingest ==")
    results = oingest.ingest_map_file(conn, args.map_file, seqdata_root=args.seqdata_root,
                                      metadata_root=args.metadata_root)
    n_fail = 0
    new_projects = []
    for project_id, findings, status in results:
        print(f"[{icons[status]}] {project_id}")
        for f in findings:
            print(f"    {f.level}: {f.message}")
        if status == "fail":
            n_fail += 1
        else:
            new_projects.append(project_id)
    print(f"ingested {len(results)} project(s), {n_fail} rejected (FAIL)")

    if not args.skip_integrity:
        print("\n== integrity ==")
        if not new_projects:
            print("(no newly ingested projects to check)")
        for pid in new_projects:
            res = ointegrity.check_catalog_integrity(
                conn, seqdata_root=args.seqdata_root, only_project=pid, jobs=args.jobs)
            s = res.get(pid)
            if s is None:
                print(f"[--] {pid}: no files")
                continue
            print(f"[{icons[s['status']]}] {pid}: {s['n_files']} file(s), "
                  f"{s['n_ok']} ok, {s['n_gzip_error']} gzip-error, "
                  f"{s['n_format_error']} format-error, {s['n_unchecked']} unchecked")
            for w in s["parity_warnings"]:
                print(f"    WARN: read-count parity: {w}")

    if not args.skip_taxonomy:
        print("\n== taxonomy resolve ==")
        taxdir = args.taxdir or _default_taxdir(args.db)
        tax_results = otax.resolve_catalog(conn, taxdir, redo=False)
        review = os.path.join(os.path.dirname(os.path.abspath(args.db)) or ".",
                              "taxonomy_review.csv")
        otax.write_review_csv(tax_results, review)
        n_flag = sum(1 for d in tax_results if d["match_type"] != "exact")
        print(f"resolved {len(tax_results)} taxa ({n_flag} fuzzy/unresolved)")
        print(f"review + edit confirmed_taxid in: {review}")
        print(f"then: odna.py --db {args.db} taxonomy apply --review {review}")

    conn.close()
    print("\nonboard complete.")


def cmd_query(args):
    conn = odb.connect(args.db)
    if args.what == "uniq-id":
        _print_rows(oquery.find_by_uniq_id(conn, args.term),
                    ["project_id", "sample_id", "taxon", "uniq_id"])
    elif args.what == "search":
        _print_rows(oquery.find_sample(conn, args.term),
                    ["project_id", "sample_id", "taxon", "uniq_id"])
    elif args.what == "unbacked":
        _print_rows(oquery.unbacked_projects(conn),
                    ["project_id", "verified", "n_files", "n_mismatch"])
    elif args.what == "mismatches":
        _print_rows(oquery.mismatched_files(conn),
                    ["project_id", "filename", "store_md5", "pdrive_md5"])
    elif args.what == "taxa":
        _print_rows(oquery.unresolved_taxa(conn),
                    ["taxon", "match_type", "taxid", "sci_name", "rank"])
    elif args.what == "summary":
        _print_rows(oquery.project_summary(conn),
                    ["project_id", "source", "n_samples", "n_files",
                     "data_check_status", "n_mismatch", "n_uncompared", "owner_name"])
    conn.close()


def _default_taxdir(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)) or ".", ".taxonomy")


def cmd_taxonomy(args):
    from odna import taxonomy as otax
    conn = odb.connect(args.db)
    odb.init_db(conn)
    taxdir = args.taxdir or _default_taxdir(args.db)
    if args.action == "resolve":
        if args.force_download:
            otax.ensure_taxdump(taxdir, force=True)
            otax.build_index(taxdir, force=True)
        elif args.rebuild_index:
            otax.build_index(taxdir, force=True)
        results = otax.resolve_catalog(conn, taxdir, redo=args.redo)
        review = os.path.join(os.path.dirname(os.path.abspath(args.db)) or ".",
                              "taxonomy_review.csv")
        otax.write_review_csv(results, review)
        n_flag = sum(1 for d in results if d["match_type"] != "exact")
        print(f"resolved {len(results)} taxa ({n_flag} fuzzy/unresolved)")
        print(f"review + edit confirmed_taxid in: {review}")
        print(f"then: odna.py --db {args.db} taxonomy apply --review {review}")
    elif args.action == "apply":
        n = otax.apply_review(conn, taxdir, args.review)
        print(f"applied {n} confirmed taxid(s)")
    conn.close()


def cmd_gui(args):
    from odna import gui as ogui
    ogui.launch(args.db, port=args.port)


def build_parser():
    p = argparse.ArgumentParser(description="Ocean DNA catalog CLI")
    p.add_argument("--db", default="oceandna_catalog.db", help="path to catalog SQLite DB")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db").set_defaults(func=cmd_init_db)

    pi = sub.add_parser("ingest", help="load CSV map files")
    pi.add_argument("map_file", help="two-column map file (metadata csv, data dir)")
    pi.add_argument("--metadata-root",
                    help="dir holding the per-project metadata CSVs (default: map file's dir)")
    pi.add_argument("--seqdata-root", help="optional root of raw_sequence_data for disk checks")
    pi.set_defaults(func=cmd_ingest)

    pc = sub.add_parser("checksums", help="load + compare rclone md5sum output")
    pc.add_argument("--store", required=True, help="rclone md5sum output for Store")
    pc.add_argument("--pdrive", required=True, help="rclone md5sum output for P-drive")
    pc.add_argument("--project", help="limit to one project_id")
    pc.add_argument("--source", default="backfill", choices=["ingest", "backfill"])
    pc.set_defaults(func=cmd_checksums)

    pv = sub.add_parser("validate", help="re-check the catalog (data-files + checksum)")
    pv.add_argument("--seqdata-root",
                    help="root of raw_sequence_data; enables the on-disk data-files check")
    pv.set_defaults(func=cmd_validate)

    pin = sub.add_parser("integrity", help="gzip + FASTQ integrity check of cataloged files")
    pin.add_argument("--seqdata-root",
                     help="root of raw_sequence_data (default: each project's stored root)")
    pin.add_argument("--project", help="limit to one project_id")
    pin.add_argument("--jobs", type=int, default=None,
                     help="concurrent workers (default: min(8, CPU count))")
    pin.set_defaults(func=cmd_integrity)

    po = sub.add_parser("onboard",
                        help="new batch: ingest + integrity + taxonomy resolve in one go")
    po.add_argument("map_file", help="two-column map file (metadata csv, data dir)")
    po.add_argument("--metadata-root",
                    help="dir holding the per-project metadata CSVs (default: map file's dir)")
    po.add_argument("--seqdata-root", required=True,
                    help="root of raw_sequence_data (files must be on disk for integrity)")
    po.add_argument("--jobs", type=int, default=None, help="integrity worker count")
    po.add_argument("--taxdir", help="taxdump dir (default: <db dir>/.taxonomy)")
    po.add_argument("--skip-integrity", action="store_true", help="skip the integrity step")
    po.add_argument("--skip-taxonomy", action="store_true", help="skip the taxonomy step")
    po.set_defaults(func=cmd_onboard)

    pq = sub.add_parser("query", help="lookups")
    pq.add_argument("what", choices=["uniq-id", "search", "unbacked", "mismatches",
                                     "summary", "taxa"])
    pq.add_argument("term", nargs="?", default="")
    pq.set_defaults(func=cmd_query)

    pt = sub.add_parser("taxonomy", help="resolve sample taxa to NCBI TaxIDs")
    tsub = pt.add_subparsers(dest="action", required=True)
    tr = tsub.add_parser("resolve",
                         help="download/index taxdump, resolve taxa, write review CSV")
    tr.add_argument("--taxdir", help="taxdump dir (default: <db dir>/.taxonomy)")
    tr.add_argument("--force-download", action="store_true", help="re-download the taxdump")
    tr.add_argument("--rebuild-index", action="store_true", help="rebuild the taxdump index")
    tr.add_argument("--redo", action="store_true", help="re-resolve confirmed taxa too")
    ta = tsub.add_parser("apply", help="apply confirmed_taxid overrides from a review CSV")
    ta.add_argument("--review", required=True, help="edited taxonomy_review.csv")
    ta.add_argument("--taxdir", help="taxdump dir (default: <db dir>/.taxonomy)")
    pt.set_defaults(func=cmd_taxonomy)

    pg = sub.add_parser("gui", help="launch Streamlit browse GUI")
    pg.add_argument("--port", type=int, default=8501)
    pg.set_defaults(func=cmd_gui)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Ocean DNA catalog CLI.

Subcommands:
  init-db     create the catalog schema
  ingest      load CSV map files (metadata only) into the catalog
  checksums   load + compare `rclone md5sum` output from Store and P-drive
  validate    re-check the catalog and print findings
  query       lookups (uniq-id, search, unbacked, mismatches, summary)
  gui         launch the Streamlit browse GUI (prints SSH tunnel command)
"""

import argparse
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
    results = oingest.ingest_map_file(conn, args.map_file, seqdata_root=args.seqdata_root)
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


def cmd_validate(args):
    conn = odb.connect(args.db)
    results = ovalidate.validate_catalog(conn)
    conn.close()
    if not results:
        print("no problems found")
        return
    for project_id in sorted(results):
        print(project_id)
        for f in results[project_id]:
            print(f"    {f.level}: {f.message}")


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
    elif args.what == "summary":
        _print_rows(oquery.project_summary(conn),
                    ["project_id", "source", "n_samples", "n_files", "verified"])
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
    pi.add_argument("--seqdata-root", help="optional root of raw_sequence_data for disk checks")
    pi.set_defaults(func=cmd_ingest)

    pc = sub.add_parser("checksums", help="load + compare rclone md5sum output")
    pc.add_argument("--store", required=True, help="rclone md5sum output for Store")
    pc.add_argument("--pdrive", required=True, help="rclone md5sum output for P-drive")
    pc.add_argument("--project", help="limit to one project_id")
    pc.add_argument("--source", default="backfill", choices=["ingest", "backfill"])
    pc.set_defaults(func=cmd_checksums)

    sub.add_parser("validate", help="re-check the catalog").set_defaults(func=cmd_validate)

    pq = sub.add_parser("query", help="lookups")
    pq.add_argument("what", choices=["uniq-id", "search", "unbacked", "mismatches", "summary"])
    pq.add_argument("term", nargs="?", default="")
    pq.set_defaults(func=cmd_query)

    pg = sub.add_parser("gui", help="launch Streamlit browse GUI")
    pg.add_argument("--port", type=int, default=8501)
    pg.set_defaults(func=cmd_gui)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Ocean DNA catalog CLI.

Subcommands:
  init-db     create the catalog schema
  ingest      load CSV map file(s); auto-run integrity + taxonomy, refresh data-files check
  checksums   load + compare `rclone md5sum` output from Store and P-drive
  validate    re-check the catalog and print findings
  integrity   gzip + FASTQ structural integrity check of cataloged files
  taxonomy    resolve sample taxa to NCBI TaxIDs (resolve / apply)
  query       lookups (uniq-id, search, unbacked, mismatches, summary, taxa)
  gui         launch the Streamlit browse GUI (prints SSH tunnel command)
"""

import argparse
import os
import re
import shlex
import subprocess
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


def _has_unchecked_files(conn, project_id):
    """True if the project has any file that integrity has never checked."""
    return conn.execute(
        "SELECT 1 FROM files WHERE project_id=? AND integrity_status IS NULL LIMIT 1",
        (project_id,)).fetchone() is not None


def _has_unresolved_taxa(conn):
    """True if any sample taxon has no row in the taxa table yet.

    Gates the taxonomy step on *new* taxa (a fresh name, or one changed to a
    string never resolved before) rather than on confirmation status -- taxa
    stay unconfirmed until a manual `taxonomy apply`, so gating on `confirmed`
    would re-resolve (and reload the taxdump) on every re-ingest. When the step
    does run, resolve_catalog(redo=False) still refreshes all unconfirmed taxa.
    """
    return conn.execute(
        """SELECT 1 FROM samples s LEFT JOIN taxa t ON t.taxon = s.taxon
           WHERE s.taxon IS NOT NULL AND s.taxon != ''
             AND t.taxon IS NULL LIMIT 1""").fetchone() is not None


def cmd_ingest(args):
    """Ingest a map file, then auto-run integrity + taxonomy and refresh the check.

    Re-ingesting a project upserts its rows (Taxon edits included). The pipeline
    steps are gated so unchanged re-runs are cheap: integrity only touches
    projects with never-checked files, taxonomy only runs when unresolved taxa
    exist. Samples dropped from a CSV are reported but left in place unless
    --prune is given, which deletes samples/files the corrected CSV no longer
    references. A final data-files check refreshes the stored data_check_issues
    + checksum/status (with --seqdata-root it re-validates against disk; without,
    it just clears rows made stale by a --prune) so the report and GUI reflect
    the ingest without a separate `validate` run.
    """
    from odna import integrity as ointegrity
    from odna import taxonomy as otax
    icons = {"pass": "OK", "warn": "WARN", "fail": "FAIL"}
    conn = odb.connect(args.db)
    odb.init_db(conn)

    print("== ingest ==")
    results = oingest.ingest_map_file(conn, args.map_file, seqdata_root=args.seqdata_root,
                                      metadata_root=args.metadata_root, prune=args.prune)
    n_fail = 0
    ingested = []  # project_ids that did not FAIL
    tot_new = tot_changed = tot_files = 0
    tot_pruned_s = tot_pruned_f = 0
    for project_id, findings, status, stats in results:
        print(f"[{icons[status]}] {project_id}")
        for f in findings:
            print(f"    {f.level}: {f.message}")
        if status == "fail":
            n_fail += 1
            continue
        ingested.append(project_id)
        tot_new += stats["new_samples"]
        tot_changed += stats["changed_samples"]
        tot_files += stats["new_files"]
        tot_pruned_s += len(stats["pruned_samples"])
        tot_pruned_f += stats["pruned_files"]
        if args.prune:
            for sid in stats["pruned_samples"]:
                print(f"    pruned: sample {sid} (dropped from CSV) and its files")
            if stats["pruned_files"]:
                print(f"    pruned: {stats['pruned_files']} stale file row(s) no longer in CSV")
        else:
            for sid in stats["orphan_samples"]:
                print(f"    WARN: sample {sid} in catalog but not in this CSV "
                      f"(kept; re-run with --prune to remove)")
    print(f"ingested {len(results)} project(s), {n_fail} rejected (FAIL); "
          f"{tot_new} new sample(s), {tot_changed} changed, {tot_files} new file(s)")
    if args.prune and (tot_pruned_s or tot_pruned_f):
        print(f"pruned {tot_pruned_s} sample(s) and {tot_pruned_f} stale file row(s)")

    if not args.skip_integrity:
        print("\n== integrity ==")
        if not args.seqdata_root:
            print("(skipped: pass --seqdata-root to check files on disk)")
        else:
            pending = [pid for pid in ingested if _has_unchecked_files(conn, pid)]
            if not pending:
                print("(no new/unchecked files)")
            for pid in pending:
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
        if not _has_unresolved_taxa(conn):
            print("(no new taxa to resolve)")
        else:
            taxdir = args.taxdir or _default_taxdir(args.db)
            tax_results = otax.resolve_catalog(conn, taxdir, redo=False)
            review = os.path.join(os.path.dirname(os.path.abspath(args.db)) or ".",
                                  "taxonomy_review.csv")
            otax.write_review_csv(tax_results, review)
            n_flag = sum(1 for d in tax_results if d["match_type"] != "exact")
            print(f"resolved {len(tax_results)} taxa ({n_flag} fuzzy/unresolved)")
            if tax_results:
                print(f"review + edit confirmed_taxid in: {review}")
                print(f"then: odna.py --db {args.db} taxonomy apply --review {review}")

    # Refresh the stored data-files + checksum state. Ingest changes what's
    # cataloged -- new/changed rows and, with --prune, deletions -- so the
    # data_check_issues + counts (read by `validate` and the GUI) would
    # otherwise stay stale until a manual `validate`. This is the step that
    # makes a --prune actually clear the removed files from the report.
    pruned_pids = [pid for pid, _, _, s in results
                   if s["pruned_samples"] or s["pruned_files"]]
    if ingested:
        print("\n== data-files check ==")
        if args.seqdata_root:
            val = ovalidate.validate_catalog(conn, seqdata_root=args.seqdata_root)
            for pid in ingested:
                r = val.get(pid)
                if r:
                    tag = "pass" if r["data"]["status"] == "ok" else "warn"
                    print(f"[{icons[tag]}] {pid}: data-files {_data_label(r['data'])}, "
                          f"checksum {_checksum_label(r['checksum'])}")
        elif pruned_pids:
            # No disk access to recompute; drop the now-stale issue rows for
            # pruned projects so the report does not show removed files.
            for pid in pruned_pids:
                conn.execute("DELETE FROM data_check_issues WHERE project_id=?", (pid,))
                conn.execute(
                    "UPDATE projects SET data_check_status='unchecked', "
                    "data_check_n_missing=NULL, data_check_n_orphan=NULL WHERE project_id=?",
                    (pid,))
            conn.commit()
            print("cleared stale data-files issues for pruned project(s); "
                  "pass --seqdata-root (or run `validate --seqdata-root`) to recompute")
        else:
            print("(skipped: pass --seqdata-root to check files on disk)")

    conn.close()
    print("\ningest complete.")


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


def _print_integrity(results):
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


def _safe_name(s):
    """A filename/job-name-safe slug for a project_id."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def _batch_script(name, log_path, slots, mem, mres, run_cmd):
    """A Hydra qsub script that checks one project on the I/O queue (lTIO.sq).

    The I/O queue is the only way to reach the NAS/Store partition from a compute
    node (see the SI HPC "NAS Storage and the I/O Queue" wiki). Integrity is a
    read-every-byte scan, so it fits the queue's data-movement intent. lTIO caps:
    72h wall, 12h CPU/slot, 8G/slot, 6 slots and 2 concurrent jobs per user.
    """
    return f"""#!/bin/bash
#$ -N odna_int_{name}
#$ -o {log_path}
#$ -j y
#$ -terse
#$ -notify
#$ -pe mthread {slots}
#$ -q lTIO.sq -l ioq
#$ -l mres={mres}G,h_data={mem}G,h_vmem={mem}G
#$ -S /bin/bash
#$ -cwd

echo + `date` $JOB_NAME running on $HOSTNAME in $QUEUE with jobID=$JOB_ID
source ~/.bashrc
conda activate odna
{run_cmd}
status=$?
echo = `date` $JOB_NAME done exit=$status
exit $status
"""


def _submit_batch(args, projects):
    """Write a per-project qsub script and (unless --no-submit) submit it.

    Each remote job checks one project and writes its results to
    <batch-dir>/results/<project>.json instead of the DB, so no two Hydra nodes
    ever write the shared SQLite catalog at once. Merge them afterwards with
    `integrity --collect <batch-dir>/results`.
    """
    if not projects:
        print("no projects with cataloged files to submit")
        return

    batch_dir = os.path.abspath(args.batch_dir)
    scripts_dir = os.path.join(batch_dir, "scripts")
    logs_dir = os.path.join(batch_dir, "logs")
    results_dir = os.path.join(batch_dir, "results")
    for d in (scripts_dir, logs_dir, results_dir):
        os.makedirs(d, exist_ok=True)

    odna_py = os.path.abspath(__file__)
    db_path = os.path.abspath(args.db)
    slots, mem = args.slots, args.mem
    mres = slots * mem

    job_ids = []
    for pid in projects:
        safe = _safe_name(pid)
        script_path = os.path.join(scripts_dir, f"integrity_{safe}.job")
        out_json = os.path.join(results_dir, f"{safe}.json")
        log_path = os.path.join(logs_dir, f"integrity_{safe}.log")
        cmd = ["python", shlex.quote(odna_py), "--db", shlex.quote(db_path),
               "integrity", "--project", shlex.quote(pid),
               "--emit-json", shlex.quote(out_json), "--jobs", str(slots)]
        if args.seqdata_root:
            cmd += ["--seqdata-root", shlex.quote(os.path.abspath(args.seqdata_root))]
        if args.force:
            cmd.append("--force")
        with open(script_path, "w") as fh:
            fh.write(_batch_script(safe, log_path, slots, mem, mres, " ".join(cmd)))
        os.chmod(script_path, 0o755)

        if args.no_submit:
            print(f"wrote {script_path}")
            continue
        try:
            out = subprocess.run(["qsub", script_path], capture_output=True, text=True)
        except FileNotFoundError:
            print("qsub not found on PATH -- scripts written but not submitted.")
            print(f"submit them on the Hydra head node, e.g.: qsub {script_path}")
            args.no_submit = True
            break
        if out.returncode != 0:
            print(f"qsub failed for {pid}: {out.stderr.strip()}")
            continue
        jid = out.stdout.strip()
        job_ids.append(jid)
        print(f"submitted {pid}: job {jid}  ({script_path})")

    print()
    if args.no_submit:
        print(f"generated {len(projects)} script(s) in {scripts_dir}")
    else:
        print(f"submitted {len(job_ids)} job(s) to lTIO.sq")
    print("when the jobs finish, merge their results into the catalog:")
    print(f"  python {odna_py} --db {db_path} integrity --collect {results_dir}")


def cmd_integrity(args):
    from odna import integrity as ointegrity
    conn = odb.connect(args.db)
    odb.init_db(conn)

    if args.collect:
        summaries = ointegrity.collect_json(conn, args.collect)
        conn.close()
        _print_integrity(summaries)
        return

    if args.emit_json:
        if not args.project:
            conn.close()
            sys.exit("integrity --emit-json requires --project")
        ointegrity.emit_project_json(
            conn, args.project, args.emit_json, seqdata_root=args.seqdata_root,
            jobs=args.jobs, recheck=args.force)
        conn.close()
        return

    if args.batch:
        projects = ointegrity.list_projects(conn, args.project)
        conn.close()
        _submit_batch(args, projects)
        return

    results = ointegrity.check_catalog_integrity(
        conn, seqdata_root=args.seqdata_root, only_project=args.project, jobs=args.jobs,
        recheck=args.force)
    conn.close()
    _print_integrity(results)


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

    pi = sub.add_parser(
        "ingest",
        help="load CSV map file(s); auto-run integrity + taxonomy, refresh data-files check")
    pi.add_argument("map_file", help="two-column map file (metadata csv, data dir)")
    pi.add_argument("--metadata-root",
                    help="dir holding the per-project metadata CSVs (default: map file's dir)")
    pi.add_argument("--seqdata-root",
                    help="root of raw_sequence_data; enables disk checks + the integrity step")
    pi.add_argument("--jobs", type=int, default=None, help="integrity worker count")
    pi.add_argument("--taxdir", help="taxdump dir (default: <db dir>/.taxonomy)")
    pi.add_argument("--skip-integrity", action="store_true", help="skip the integrity step")
    pi.add_argument("--skip-taxonomy", action="store_true", help="skip the taxonomy step")
    pi.add_argument("--prune", action="store_true",
                    help="delete catalog samples/files the CSV no longer lists "
                         "(then re-run `validate --seqdata-root`)")
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
                     help="concurrent read streams (default: min(8, CPU count); on a "
                          "network mount try 16-32 to fill the pipe)")
    pin.add_argument("--force", action="store_true",
                     help="re-read every file, even ones that already passed and are "
                          "unchanged (default: skip those for a fast, resumable run)")
    pin.add_argument("--batch", action="store_true",
                     help="generate a per-project qsub script and submit it to Hydra's "
                          "I/O queue (lTIO.sq) for remote checking (the only way to reach "
                          "the NAS/Store partition from a compute node); each job writes "
                          "JSON results to merge later with --collect. Respects --project.")
    pin.add_argument("--batch-dir", default="integrity_batch",
                     help="dir for generated batch scripts/logs/results "
                          "(default: ./integrity_batch)")
    pin.add_argument("--slots", type=int, default=4,
                     help="mthread slots per batch job, also the remote --jobs "
                          "(default 4; lTIO caps 6 slots/user and 2 concurrent jobs)")
    pin.add_argument("--mem", type=int, default=2,
                     help="memory (GB) requested per slot for batch jobs "
                          "(default 2; lTIO caps 8G/slot)")
    pin.add_argument("--no-submit", action="store_true",
                     help="with --batch, write the qsub scripts but do not run qsub")
    pin.add_argument("--emit-json", metavar="PATH",
                     help="(used by batch jobs) check one --project and write results to "
                          "PATH as JSON instead of writing the DB")
    pin.add_argument("--collect", metavar="DIR",
                     help="merge batch result JSON files from DIR into the catalog and "
                          "print the per-project summary")
    pin.set_defaults(func=cmd_integrity)

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

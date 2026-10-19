"""Ocean DNA sequence data catalog CLI.

Subcommands:
  init-db     create the catalog schema
  ingest      load metadata (auto-discover); auto-run taxonomy, refresh data-files check
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
import sqlite3
import subprocess
import sys
import tarfile
from datetime import date
from urllib.error import URLError

from seqledger import db as odb
from seqledger import ingest as oingest
from seqledger import checksums as ochecksums
from seqledger import validate as ovalidate


def _print_rows(rows, cols):
    if not rows:
        print("(none)", file=sys.stderr)
        return
    print("\t".join(cols))
    for r in rows:
        print("\t".join("" if r[c] is None else str(r[c]) for c in cols))


def _require_db(db_path, db_given=True):
    """Exit with a plain message (not a traceback) if the catalog doesn't exist."""
    if not os.path.exists(db_path):
        how = ""
        if not db_given:
            how = (" (no --db given; using $SEQLEDGER_DB)" if os.environ.get("SEQLEDGER_DB")
                   else " (no --db given and $SEQLEDGER_DB is not set)")
        sys.exit(f"no catalog at '{db_path}'{how}. Check the path, pass --db, or set "
                 "SEQLEDGER_DB (ask your data manager for the catalog path).\n"
                 "Data managers: create one with: seqledger --db PATH init-db")


def _resolve_root(conn, value, key, label, verbose=0):
    """A directory root: the CLI flag if given, else the catalog's configured value.

    seqdata_root / metadata_root are stored at init-db, so ingest/validate/integrity
    can be run without repeating them. At -v, notes (on stderr) when the configured
    value is used so it's clear where the path came from. Returns None if neither is set.
    """
    if value:
        return value
    cfg = odb.get_config(conn, key)
    if cfg:
        if verbose:
            print(f"(using configured {label}: {cfg})", file=sys.stderr)
        return cfg
    return None


# init-db flag name -> config key; other keys go through --set KEY=VALUE.
_INIT_CONFIG_FLAGS = {"name": "catalog_name", "seqdata_root": "seqdata_root",
                      "metadata_root": "metadata_root"}


def cmd_init_db(args):
    if args.show:
        conn = odb.connect_ro(args.db)
        print("config (defaults + stored):")
        for k, v in sorted(odb.resolve_config(conn).items()):
            print(f"  {k} = {v}")
        conn.close()
        return 0
    conn = odb.connect(args.db)
    odb.init_db(conn)

    # Apply any config the user supplied (flags + repeatable --set KEY=VALUE).
    updates = {}
    for flag, key in _INIT_CONFIG_FLAGS.items():
        val = getattr(args, flag, None)
        if val is not None:
            updates[key] = val
    for pair in (args.set or []):
        if "=" not in pair:
            conn.close()
            sys.exit(f"--set expects KEY=VALUE, got: {pair}")
        k, v = pair.split("=", 1)
        updates[k.strip()] = v
    try:
        for k, v in updates.items():
            odb.set_config(conn, k, v)
    except ValueError as e:
        conn.close()
        sys.exit(str(e))
    conn.commit()

    conn.close()
    msg = f"initialized schema in {args.db}"
    if updates:
        msg += f"; set {len(updates)} config value(s)"
    print(msg)


def _has_unchecked_files(conn, project_id):
    """True if the project has any file that integrity has never checked."""
    return conn.execute(
        "SELECT 1 FROM files WHERE project_id=? AND integrity_status IS NULL LIMIT 1",
        (project_id,)).fetchone() is not None


def _has_unresolved_taxa(conn):
    """True if any sample taxon was never resolved against NCBI.

    Gates the taxonomy step on *new* taxa (a fresh name, or one changed to a
    string never resolved before) rather than on confirmation status -- taxa
    stay unconfirmed until a manual `taxonomy apply`, so gating on `confirmed`
    would re-resolve (and reload the taxdump) on every re-ingest. When the step
    does run, resolve_catalog(scope="new") resolves only those new taxa.
    """
    return conn.execute(
        """SELECT 1 FROM samples s LEFT JOIN taxa t ON t.taxon = s.taxon
           WHERE s.taxon IS NOT NULL AND s.taxon != ''
             AND t.date_resolved IS NULL LIMIT 1""").fetchone() is not None


def _review_path(base, name):
    """<base>/<name>.csv, or a dated name if that file already exists."""
    path = os.path.join(base, f"{name}.csv")
    if not os.path.exists(path):
        return path
    stem = os.path.join(base, f"{name}_{date.today().isoformat()}")
    path, n = f"{stem}.csv", 2
    while os.path.exists(path):
        path, n = f"{stem}_{n}.csv", n + 1
    return path


def _prune_guard(prune, prune_projects, yes, isatty, ask=input):
    """Gate a destructive --prune / --prune-projects run.

    Returns None to proceed; returns an error message (caller prints + exits
    nonzero) to stop. --yes always proceeds. Interactively (a TTY) the user is
    prompted and a non-yes answer aborts. Non-interactively (cron / pipe) the run
    is REFUSED outright -- isatty() is False there, so without this it would delete
    rows with no confirmation at all.
    """
    if not (prune or prune_projects) or yes:
        return None
    flags = " + ".join(f for f, on in
                       (("--prune", prune), ("--prune-projects", prune_projects)) if on)
    if not isatty:
        return (f"refusing destructive {flags} in a non-interactive run; "
                "pass --yes to confirm")
    resp = ask(f"{flags} will DELETE catalog rows the source(s) no longer list "
               "(check for a truncated CSV first). Continue? [y/N] ")
    if resp.strip().lower() not in ("y", "yes"):
        return "aborted."
    return None


def cmd_ingest(args):
    """Ingest metadata, then auto-run taxonomy resolve and refresh the data-files check.

    Re-ingesting a project upserts its rows (Taxon edits included). The follow-on
    steps are gated so unchanged re-runs are cheap: taxonomy only runs when
    unresolved taxa exist. Samples dropped from a CSV are reported but left in
    place unless --prune is given, which deletes samples/files the corrected CSV no
    longer references. A final data-files check refreshes the stored
    data_check_issues + checksum/status so the report and GUI reflect the ingest
    without a separate `validate` run.

    Integrity (the gzip/FASTQ byte-scan) is deliberately NOT run here -- it reads
    every byte and is slow, so it is a separate opt-in step: run `seqledger integrity`
    (or `integrity --batch` on Hydra's I/O queue) when you want it.
    """
    from seqledger import taxonomy as otax
    v = args.verbose
    conn = odb.connect(args.db)
    odb.init_db(conn)

    # Fall back to the roots configured at init-db when the flags aren't given.
    args.seqdata_root = _resolve_root(conn, args.seqdata_root, "seqdata_root", "seqdata-root", v)
    args.metadata_root = _resolve_root(conn, args.metadata_root, "metadata_root",
                                       "metadata-root", v)

    # Destructive: confirm before deleting rows a (possibly truncated) source no
    # longer lists. Bypass with --yes for scripts / non-interactive runs; a
    # non-interactive run WITHOUT --yes is refused (never silently deletes).
    guard = _prune_guard(args.prune, args.prune_projects, args.yes, sys.stdin.isatty())
    if guard:
        conn.close()
        print(guard, file=sys.stderr)
        return 2

    # Fail loudly (not silently-empty) when a root is missing/unmounted/mistyped --
    # the most likely first-run mistake, which otherwise reports "ingest complete"
    # over an empty catalog.
    def _need_dir(path, what):
        if path and not os.path.isdir(path):
            conn.close()
            sys.exit(f"{what} '{path}' does not exist or is not a directory -- check "
                     "the path and that the filesystem (Store/NAS) is mounted.")

    if not args.seqdata_root or not args.metadata_root:
        conn.close()
        sys.exit("ingest needs both --seqdata-root and --metadata-root")
    _need_dir(args.seqdata_root, "--seqdata-root")
    _need_dir(args.metadata_root, "--metadata-root")
    results = oingest.ingest_tree(
        conn, args.seqdata_root, args.metadata_root, prune=args.prune)

    if not results:
        print("WARNING: discovered 0 projects -- nothing was ingested. Check that "
              "--seqdata-root has project folders and --metadata-root has "
              "'<project>_mapfile.csv' files (and that the mount is present).")
    n_fail = 0
    refused = False
    ingested = []  # project_ids that did not FAIL
    rows = {}  # project_id -> (problem parts, disk findings, other findings)
    notes = []  # extra summary lines
    tot_new = tot_changed = tot_files = 0
    tot_pruned_s = tot_pruned_f = 0
    for project_id, findings, status, stats in results:
        msgs = [f.message for f in findings]
        disk = [m for m in msgs if "not found on disk" in m or "on disk is not referenced" in m]
        other = [m for m in msgs if m not in disk]
        parts = ["rejected"] if status == "fail" else []
        parts += other if len(other) == 1 else [f"{len(other)} mapfile warnings"] if other else []
        rows[project_id] = (parts, disk, other)
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
            other += [f"pruned: sample {sid} (dropped from CSV) and its files"
                      for sid in stats["pruned_samples"]]
        elif stats["orphan_samples"]:
            parts.append(f"{len(stats['orphan_samples'])} sample(s) in catalog, not in "
                         "mapfile (kept; --prune removes)")
            other += [f"sample {sid} in catalog but not in this CSV "
                      "(kept; re-run with --prune to remove)" for sid in stats["orphan_samples"]]

    n_gone = 0
    if args.prune_projects:
        pr = oingest.prune_missing_projects(conn, args.seqdata_root, args.metadata_root)
        if pr["skipped"]:
            refused = True
            notes.append(f"refusing to prune projects: {pr['reason']}")
        elif pr["pruned"]:
            notes.append(f"deleted {len(pr['pruned'])} vanished project(s) "
                         f"(gone from disk and mapfiles): {', '.join(pr['pruned'])}")
        else:
            notes.append("no vanished projects to prune")
    else:
        gone = oingest.prune_missing_projects(
            conn, args.seqdata_root, args.metadata_root, dry_run=True)["pruned"]
        n_gone = len(gone)
        if gone:
            more = f" ... +{n_gone - 10} more" if n_gone > 10 else ""
            notes.append(f"{n_gone} project(s) gone from disk and mapfiles: "
                         f"{', '.join(gone[:10])}{more} (kept; --prune-projects removes them)")
    n_kept = 0 if args.prune else sum(
        len(s.get("orphan_samples", ())) for _, _, st, s in results if st != "fail")
    args.prunable = (n_kept, n_gone)

    if not args.skip_taxonomy and _has_unresolved_taxa(conn):
        taxdir = args.taxdir or _default_taxdir(args.db)
        # The metadata ingest above is already committed. Resolving new taxa may
        # download the NCBI taxdump (first run / offline node); don't let that
        # failure surface as a traceback that makes the user think ingest failed.
        try:
            tax_results = otax.resolve_catalog(conn, taxdir, scope="new")
        except (OSError, URLError, tarfile.TarError, RuntimeError) as e:
            tax_results = None
            notes.append(f"Species names: taxonomy skipped ({e}); ingest succeeded, run "
                         f"`seqledger --db {args.db} taxonomy resolve` when ready")
        if tax_results:
            n_flag = sum(1 for d in tax_results if d["match_type"] != "exact")
            review = _review_path(
                os.path.dirname(os.path.abspath(args.db)) or ".", "taxonomy_review")
            otax.write_review_csv(tax_results, review)
            notes.append(f"Species names: {len(tax_results)} looked up, "
                         + (f"{n_flag} need a check -> {os.path.basename(review)} "
                            "(next to the catalog)" if n_flag else "all matched"))
            if n_flag and v:
                notes.append(f"  edit confirmed_taxid in {review}, then: "
                             f"seqledger --db {args.db} taxonomy apply --review {review}")

    # Refresh the stored data-files + checksum state. Ingest changes what's
    # cataloged -- new/changed rows and, with --prune, deletions -- so the
    # data_check_issues + counts (read by `validate` and the GUI) would
    # otherwise stay stale until a manual `validate`. This is the step that
    # makes a --prune actually clear the removed files from the report.
    val = {}
    if ingested:
        val = ovalidate.validate_catalog(conn, seqdata_root=args.seqdata_root)

    root_flag = ("" if odb.get_config(conn, "seqdata_root")
                 else " --seqdata-root <raw_sequence_data>")
    conn.close()

    n_ok = unverified = 0
    out = []
    for pid, (parts, disk, other) in sorted(rows.items()):
        r = val.get(pid) if pid in ingested else None
        if r:
            if r["data"]["status"] == "issues" or r["data"].get("detail"):
                parts.append(ovalidate.data_label(r["data"]))
            if r["checksum"]["status"] == "mismatch":
                parts.append(ovalidate.checksum_label(r["checksum"]))
            disk = [f.message for f in r["notes"]]
        else:
            miss = sum("not found on disk" in m for m in disk)
            if miss:
                parts.append(f"{miss} file(s) in mapfile not on disk")
            if len(disk) - miss:
                parts.append(f"{len(disk) - miss} file(s) on disk, not in mapfile")
        n_ok += not parts
        if r and r["checksum"]["status"] == "incomplete":
            unverified += 1
            if v:
                parts.append(ovalidate.checksum_label(r["checksum"]))
        out.append((pid, "; ".join(parts), [d for d in disk + other if d not in parts]))

    changed = tot_new or tot_changed or tot_files
    if changed:
        print(f"Ingest: {len(results)} project(s) ({n_ok} OK, "
              f"{len(results) - n_ok - n_fail} with problems, {n_fail} rejected)")
        print(f"  {tot_new} new sample(s), {tot_changed} changed, {tot_files} new file(s)")
    elif results:
        print(f"Ingest: {len(results)} project(s), nothing new or changed"
              + (f" ({n_fail} rejected)." if n_fail else "."))
    if args.prune and (tot_pruned_s or tot_pruned_f):
        print(f"  pruned {tot_pruned_s} sample(s) and {tot_pruned_f} stale file row(s)")
    for line in notes:
        print(f"  {line}")
    hidden = _problems(out, v)
    if unverified:
        print(f"Backups not yet verified: {unverified} project(s)")
    if changed:
        print(f"Next: seqledger --db {args.db} integrity{root_flag}   "
              "(checks files for damage; slow)")
        if v:
            print(f"  or on Hydra: seqledger --db {args.db} integrity --batch{root_flag}")
    if hidden:
        print(_MORE[v])
    return 2 if refused else 1 if n_fail else 0


def cmd_checksums(args):
    v = args.verbose
    conn = odb.connect(args.db)
    odb.init_db(conn)
    summary = ochecksums.load_checksums(
        conn, args.store, args.pdrive, source=args.source, only_project=args.project,
        full_reset=args.full, force=args.force)
    ps = summary["projects"]
    mism = [r[0] for r in conn.execute(
        f"SELECT rel_path FROM files WHERE md5_match=0 AND project_id IN "
        f"({','.join('?' * len(ps))}) ORDER BY rel_path", ps)]
    conn.close()
    by_pid = {}
    general = []
    for w in sorted(summary["warnings"]):
        m = re.match(r"(?:cataloged file ')?([^/' ]+)/", w)
        if m:
            by_pid.setdefault(m.group(1), []).append(w)
        else:
            general.append(w)
    rows = []
    for pid in sorted(set(ps) | set(by_pid)):
        ws = by_pid.get(pid, [])
        parts = [f"{n} file(s) {label}" for key, label in _CS_KINDS
                 for n in [sum(key in w for w in ws)] if n]
        rows.append((pid, "; ".join(parts), ws))
    n_bad = sum(1 for r in rows if r[1] and r[0] in ps)
    print(f"Backups: compared {summary['matched']} file(s) in {len(ps)} project(s); "
          f"{len(ps) - n_bad} verified")
    for w in general:
        print(f"  WARNING: {w}")
    _problems([(p, "md5 differs", []) for p in mism], v,
              head=f"{len(mism)} file(s) differ between Store and P-drive:")
    hidden = _problems(rows, v)
    if n_bad:
        print(f"Not verified: {n_bad} project(s). List them: seqledger --db {args.db} "
              "query unbacked")
    if hidden:
        print(_MORE[v])
    return 1 if mism else 0


_CS_KINDS = (("MISMATCH", "differ between Store and P-drive"),
             ("missing from Store", "missing from the Store listing"),
             ("missing from P-drive", "missing from the P-drive listing"),
             ("absent from both", "in neither listing (backup not yet verified)"),
             ("(orphan)", "on disk, not in mapfile"))
_MORE = ("More detail: add -v (per project) or -vv (per file)",
         "More detail: add -vv (per file)")


def _problems(rows, verbose, cap=10, head=None):
    """Print (name, problem, detail lines) rows; return True if detail was left out.

    Default: rows with a problem, capped at `cap`. -v: every row. -vv: plus detail.
    """
    shown = rows if verbose else [r for r in rows if r[1]]
    if shown:
        print(head or ("Projects:" if verbose else "Problems:"))
    for name, prob, detail in shown[:None if verbose else cap]:
        print(f"  {name}: {prob or 'OK'}")
        for d in sorted(detail, key=lambda d: d.split("'", 1)[-1]) if verbose > 1 else ():
            print(f"      {d}")
    more = 0 if verbose else len(shown) - cap
    if more > 0:
        print(f"  ... and {more} more; add -v to list all")
    return more > 0 or (verbose < 2 and any(r[2] for r in shown))


def cmd_validate(args):
    v = args.verbose
    conn = odb.connect(args.db)
    args.seqdata_root = _resolve_root(conn, args.seqdata_root, "seqdata_root",
                                      "seqdata-root", v)
    results = ovalidate.validate_catalog(conn, seqdata_root=args.seqdata_root)
    n_unscanned = 0 if args.seqdata_root else conn.execute(
        "SELECT COUNT(*) FROM projects WHERE seqdata_root IS NULL OR seqdata_root=''"
    ).fetchone()[0]
    conn.close()
    if not results:
        print("catalog is empty")
        return 0
    rows = []
    n_ok = unverified = 0
    for pid in sorted(results):
        r = results[pid]
        parts = []
        if r["data"]["status"] == "issues" or r["data"].get("detail"):
            parts.append(ovalidate.data_label(r["data"]))
        if r["checksum"]["status"] == "mismatch":
            parts.append(ovalidate.checksum_label(r["checksum"]))
        shared = sum("shared across projects" in f.message for f in r["notes"])
        if shared:
            parts.append(f"{shared} UniqID(s) shared with other projects")
        incomplete = r["checksum"]["status"] == "incomplete"
        n_ok += not parts and not incomplete
        unverified += incomplete
        if v and (incomplete or not parts):
            parts.append(ovalidate.checksum_label(r["checksum"]))
        rows.append((pid, "; ".join(parts), [f.message for f in r["notes"]]))
    print(f"Checked {len(results)} project(s): {n_ok} fully OK")
    if n_unscanned:
        print(f"  files on disk not checked for {n_unscanned} project(s) with no stored "
              "seqdata root: pass --seqdata-root to scan disk")
    hidden = _problems(rows, v)
    if unverified:
        print(f"Backups not yet verified: {unverified} project(s). List them: "
              f"seqledger --db {args.db} query unbacked")
    if hidden:
        print(_MORE[v])
    return int(any(r["data"]["status"] == "issues" or r["checksum"]["status"] == "mismatch"
                   for r in results.values()))


_INTEGRITY_LABELS = {"gzip_error": "gzip error", "format_error": "bad FASTQ line count",
                     "unchecked": "not checked (not found on disk or unreadable)"}


def _print_integrity(results, verbose=0):
    if not results:
        print("no cataloged files to check")
        return 0
    s_all = list(results.values())
    n_files = sum(s["n_files"] for s in s_all)
    n_unch = sum(s["n_unchecked"] for s in s_all)
    bad = [(p, _INTEGRITY_LABELS[st], []) for pid in sorted(results)
           for p, st in results[pid].get("bad_files", []) if st in ("gzip_error", "format_error")]
    print(f"Integrity: {n_files} file(s) in {len(results)} project(s): "
          f"{sum(s['n_ok'] for s in s_all)} OK, {len(bad)} damaged, {n_unch} not checked")
    if n_files and n_unch == n_files:
        print("  none were found on disk (is --seqdata-root correct, and are the files "
              "mounted?)")
    _problems(bad, verbose, head=f"{len(bad)} damaged file{'' if len(bad) == 1 else 's'}:")
    rows = []
    for pid in sorted(results):
        s = results[pid]
        n_bad = s["n_gzip_error"] + s["n_format_error"]
        empty = sum("0 reads" in w for w in s["parity_warnings"])
        parts = [f"{n} {label}" for n, label in (
            (n_bad if verbose else 0, "damaged file(s)"),
            (s["n_unchecked"], "file(s) not checked"),
            (empty, "empty file(s)"),
            (len(s["parity_warnings"]) - empty, "sample(s) where R1 and R2 read counts differ"))
            if n]
        detail = s["parity_warnings"] + [f"{p}: {_INTEGRITY_LABELS.get(st, st)}"
                                         for p, st in s.get("bad_files", [])]
        rows.append((pid, "; ".join(parts), detail))
    if _problems(rows, verbose):
        print(_MORE[verbose])
    return int(any(s["n_gzip_error"] or s["n_format_error"] for s in s_all))


def _safe_name(s):
    """A filename/job-name-safe slug for a project_id."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def _batch_script(name, log_path, slots, mem, run_cmd,
                  conda_env=odb.CONFIG_DEFAULTS["conda_env"],
                  io_queue=odb.CONFIG_DEFAULTS["io_queue"]):
    """A Hydra qsub script that checks one project on the I/O queue.

    The I/O queue is the only way to reach the NAS/Store partition from a compute
    node (see the SI HPC "NAS Storage and the I/O Queue" wiki). Integrity is a
    read-every-byte scan, so it fits the queue's data-movement intent. lTIO caps:
    72h wall, 12h CPU/slot, 8G/slot, 6 slots and 2 concurrent jobs per user.
    conda_env and io_queue come from the catalog config so another lab can retarget.
    """
    return odb.qsub_header(f"seqledger_int_{name}", log_path, io_queue, mem, slots) + f"""
echo + `date` $JOB_NAME running on $HOSTNAME in $QUEUE with jobID=$JOB_ID
# -notify sends SIGUSR1/2 before a wall/CPU-cap kill; log why we're stopping so a
# novice knows to just resubmit (the per-project JSON checkpoint is already saved).
trap 'echo "= `date` notified (approaching a queue cap) -- stopping; resubmit to resume"; exit 2' SIGUSR1 SIGUSR2
source ~/.bashrc
conda activate {shlex.quote(conda_env)}
{run_cmd}
status=$?
echo = `date` $JOB_NAME done exit=$status
exit $status
"""


def _submit_batch(args, projects, conda_env, io_queue):
    """Write a per-project qsub script and (unless --no-submit) submit it.

    Each remote job checks one project and writes its results to
    <batch-dir>/results/<project>.json instead of the DB, so no two Hydra nodes
    ever write the shared SQLite catalog at once. Merge them afterwards with
    `integrity --collect <batch-dir>/results`.
    """
    if not projects:
        print("no projects with cataloged files to submit")
        return 0

    batch_dir = os.path.abspath(args.batch_dir)
    scripts_dir = os.path.join(batch_dir, "scripts")
    logs_dir = os.path.join(batch_dir, "logs")
    results_dir = os.path.join(batch_dir, "results")
    for d in (scripts_dir, logs_dir, results_dir):
        os.makedirs(d, exist_ok=True)

    # Invoke the installed package (`python -m seqledger`) inside the job, so the
    # command works regardless of where the package lives on the compute node.
    run_prefix = ["python", "-m", "seqledger"]
    db_path = os.path.abspath(args.db)
    slots, mem = args.slots, args.mem

    job_ids = []
    rc = 0
    for pid in projects:
        safe = _safe_name(pid)
        script_path = os.path.join(scripts_dir, f"integrity_{safe}.job")
        out_json = os.path.join(results_dir, f"{safe}.json")
        log_path = os.path.join(logs_dir, f"integrity_{safe}.log")
        cmd = [*run_prefix, "--db", shlex.quote(db_path),
               "integrity", "--project", shlex.quote(pid),
               "--emit-json", shlex.quote(out_json), "--jobs", str(slots)]
        if args.seqdata_root:
            cmd += ["--seqdata-root", shlex.quote(os.path.abspath(args.seqdata_root))]
        if args.force:
            cmd.append("--force")
        with open(script_path, "w") as fh:
            fh.write(_batch_script(safe, log_path, slots, mem, " ".join(cmd),
                                   conda_env=conda_env, io_queue=io_queue))
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
            rc = 2
            break
        if out.returncode != 0:
            print(f"qsub failed for {pid}: {out.stderr.strip()}")
            rc = 2
            continue
        jid = out.stdout.strip()
        job_ids.append(jid)
        print(f"submitted {pid}: job {jid}  ({script_path})")

    print()
    if args.no_submit:
        print(f"generated {len(projects)} script(s) in {scripts_dir}")
    else:
        print(f"submitted {len(job_ids)} job(s) to {io_queue}")
    print("when the jobs finish, merge their results into the catalog:")
    print(f"  seqledger --db {db_path} integrity --collect {results_dir}")
    return rc


def cmd_integrity(args):
    from seqledger import integrity as ointegrity

    # The remote batch worker (--emit-json) runs on a compute node against the
    # shared master DB over NFS. It only SELECTs its file list and writes results
    # to JSON, so it MUST open read-only and MUST NOT init_db -- otherwise every
    # concurrently-scheduled job would take a write lock (and issue ALTERs on any
    # schema drift), contending or corrupting the catalog over NFS.
    if args.emit_json:
        if not args.project:
            sys.exit("integrity --emit-json requires --project")
        conn = odb.connect_ro(args.db)
        ointegrity.emit_project_json(
            conn, args.project, args.emit_json, seqdata_root=args.seqdata_root,
            jobs=args.jobs, recheck=args.force)
        conn.close()
        return

    conn = odb.connect(args.db)
    odb.init_db(conn)

    # Fall back to the seqdata root configured at init-db (used by the local run and
    # baked into batch job scripts). --collect doesn't touch disk, so it's harmless.
    args.seqdata_root = _resolve_root(conn, args.seqdata_root, "seqdata_root",
                                      "seqdata-root", args.verbose)

    if args.collect:
        summaries = ointegrity.collect_json(conn, args.collect)
        conn.close()
        return _print_integrity(summaries, args.verbose)

    if args.batch:
        projects = ointegrity.list_projects(conn, args.project)
        # File-level skip already happens inside each remote job (it reads prior
        # gz_ok/size from the DB and re-reads only changed files). --only-unchecked
        # goes further and skips *submitting a job at all* for projects that have
        # no never-checked files, so a fully-validated project costs no queue slot.
        # --force means "re-read everything", so the filter is meaningless there.
        if args.only_unchecked and not args.force:
            projects = [p for p in projects if _has_unchecked_files(conn, p)]
        conda_env = odb.get_config(conn, "conda_env")
        io_queue = odb.get_config(conn, "io_queue")
        conn.close()
        return _submit_batch(args, projects, conda_env=conda_env, io_queue=io_queue)

    results = ointegrity.check_catalog_integrity(
        conn, seqdata_root=args.seqdata_root, only_project=args.project, jobs=args.jobs,
        progress=sys.stderr.isatty(), recheck=args.force)
    conn.close()
    return _print_integrity(results, args.verbose)


_SAMPLE_COLS = ["project_id", "sample_id", "taxon", "uniq_id"]
_QUERIES = {
    "uniq-id": ("SELECT project_id, sample_id, taxon, uniq_id FROM samples "
                "WHERE uniq_id = :term ORDER BY project_id", _SAMPLE_COLS),
    "search": ("SELECT project_id, sample_id, taxon, uniq_id FROM samples "
               "WHERE sample_id LIKE :like OR taxon LIKE :like OR uniq_id LIKE :like "
               "ORDER BY project_id, sample_id", _SAMPLE_COLS),
    "unbacked": ("""SELECT p.project_id, COALESCE(b.verified, 0) AS verified,
                           b.n_files, b.n_mismatch
                    FROM projects p
                    LEFT JOIN backups b ON b.project_id = p.project_id AND b.location = :loc
                    WHERE COALESCE(b.verified, 0) = 0 ORDER BY p.project_id""",
                 ["project_id", "verified", "n_files", "n_mismatch"]),
    "mismatches": ("SELECT project_id, filename, store_md5, pdrive_md5 FROM files "
                   "WHERE md5_match = 0 ORDER BY project_id, filename",
                   ["project_id", "filename", "store_md5", "pdrive_md5"]),
    "taxa": ("""SELECT taxon, COALESCE(match_type, 'unresolved') AS match_type,
                       taxid, sci_name, rank FROM taxa
                WHERE COALESCE(match_type, 'unresolved') NOT IN ('exact', 'confirmed')
                ORDER BY match_type, taxon""",
             ["taxon", "match_type", "taxid", "sci_name", "rank"]),
    "summary": ("""SELECT p.project_id, p.source,
                   (SELECT COUNT(*) FROM samples s WHERE s.project_id = p.project_id) AS n_samples,
                   (SELECT COUNT(*) FROM files f WHERE f.project_id = p.project_id) AS n_files,
                   COALESCE(p.data_check_status, 'unchecked') AS data_check_status,
                   (SELECT COUNT(*) FROM files f
                      WHERE f.project_id = p.project_id AND f.md5_match = 0) AS n_mismatch,
                   (SELECT COUNT(*) FROM files f
                      WHERE f.project_id = p.project_id AND f.md5_match IS NULL) AS n_uncompared,
                   p.owner_name
                FROM projects p ORDER BY p.project_id""",
                ["project_id", "source", "n_samples", "n_files",
                 "data_check_status", "n_mismatch", "n_uncompared", "owner_name"]),
}


def run_query(conn, what, term=""):
    return conn.execute(_QUERIES[what][0], {
        "term": term, "like": f"%{term}%",
        "loc": odb.get_config(conn, "backup_location")}).fetchall()


def cmd_query(args):
    conn = odb.connect_ro(args.db)  # read-only: a typo can't create a stray empty DB
    _print_rows(run_query(conn, args.what, args.term), _QUERIES[args.what][1])
    conn.close()


def _default_taxdir(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)) or ".", ".taxonomy")


def _resolve_ncbi(otax, conn, args, taxdir, scope, base):
    if args.force_download:
        otax.ensure_taxdump(taxdir, force=True)
        otax.build_index(taxdir, force=True)
    elif args.rebuild_index:
        otax.build_index(taxdir, force=True)
    results = otax.resolve_catalog(conn, taxdir, scope=scope)
    n_flag = sum(1 for d in results if d["match_type"] != "exact")
    print(f"NCBI: {len(results)} taxa looked up, {n_flag} need a check")
    if not results:
        return
    review = _review_path(base, "taxonomy_review")
    otax.write_review_csv(results, review)
    if n_flag:
        print(f"Next: edit confirmed_taxid in {review}, then: "
              f"seqledger --db {args.db} taxonomy apply --review {review}")


def _resolve_worms(oworms, conn, args, taxdir, scope, base):
    # WoRMS is an online step; a network failure warns (leaving any NCBI side
    # intact) rather than aborting the whole command.
    try:
        results = oworms.resolve_catalog_worms(conn, taxdir, scope=scope)
    except (OSError, ValueError) as e:  # URLError/HTTPError subclass OSError; bad JSON -> ValueError
        print(f"WoRMS: skipped ({e}) -- network needed; retry when online.")
        return
    n_flag = sum(1 for d in results if d["worms_match_type"] not in ("exact", "confirmed"))
    print(f"WoRMS: {len(results)} taxa looked up, {n_flag} need a check")
    if not results:
        return
    review = _review_path(base, "worms_review")
    oworms.write_review_csv_worms(results, review)
    if n_flag:
        print(f"Next: edit confirmed_aphia_id in {review}, then: "
              f"seqledger --db {args.db} taxonomy apply --source worms --review {review}")


def cmd_taxonomy(args):
    from seqledger import taxonomy as otax
    from seqledger import worms as oworms
    conn = odb.connect(args.db)
    odb.init_db(conn)
    taxdir = args.taxdir or _default_taxdir(args.db)
    base = os.path.dirname(os.path.abspath(args.db)) or "."
    if args.action == "resolve":
        scope = ("all" if args.redo
                 else "unconfirmed" if args.refresh_unconfirmed else "new")
        if args.source in ("ncbi", "both"):
            _resolve_ncbi(otax, conn, args, taxdir, scope, base)
        if args.source in ("worms", "both"):
            _resolve_worms(oworms, conn, args, taxdir, scope, base)
    elif args.action == "apply":
        if args.source == "worms":
            applied, skipped = oworms.apply_review_worms(conn, taxdir, args.review)
            print(f"applied {applied} confirmed AphiaID(s)"
                  + (f", skipped {len(skipped)}" if skipped else ""))
        else:
            applied, skipped = otax.apply_review(conn, taxdir, args.review)
            print(f"applied {applied} confirmed taxid(s)"
                  + (f", skipped {len(skipped)}" if skipped else ""))
        for msg in skipped:
            print(f"    SKIP: {msg}")
    conn.close()


def cmd_gui(args):
    from seqledger import gui as ogui
    # CLI flag > catalog config > built-in default.
    cfg = odb.CONFIG_DEFAULTS
    if os.path.exists(args.db):
        # Read-only: this path only reads config to build the qsub/tunnel command
        # (no init_db/migrate/write), so it must not open the shared DB read/write.
        conn = odb.connect_ro(args.db)
        cfg = odb.resolve_config(conn)
        conn.close()
    login_host = args.login_host or cfg["login_host"]
    conda_env = args.conda_env or cfg["conda_env"]
    queue = args.queue or cfg["io_queue"]
    if args.qsub:
        ogui.launch_qsub(args.db, port=args.port, login_host=login_host,
                         queue=queue, conda_env=conda_env, mem_gb=args.mem, wait=args.wait)
    else:
        ogui.launch(args.db, port=args.port, login_host=login_host)


def build_parser():
    p = argparse.ArgumentParser(prog="seqledger", description="seqledger: sequence-data catalog CLI")
    # $SEQLEDGER_DB lets users drop --db (as the README suggests); the flag still
    # wins, and catalog.db in the cwd remains the last-resort default. Read here so
    # the env value overrides the hardcoded default instead of being ignored.
    p.add_argument("--db", default=os.environ.get("SEQLEDGER_DB") or "catalog.db",
                   help="path to catalog SQLite DB (default: $SEQLEDGER_DB, else catalog.db)")
    p.add_argument("--debug", action="store_true",
                   help="show the full traceback on error instead of a one-line message")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="more detail: -v per project, -vv per file")
    # Also accept --db / -v after the subcommand; SUPPRESS keeps the top-level value when absent.
    dbp = argparse.ArgumentParser(add_help=False)
    dbp.add_argument("--db", default=argparse.SUPPRESS, help="path to catalog SQLite DB")
    dbp.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS,
                     help="more detail: -v per project, -vv per file")
    sub = p.add_subparsers(dest="cmd")

    pdb = sub.add_parser(
        "init-db", parents=[dbp],
        help="create/upgrade the catalog schema; optionally set per-catalog config",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Create/upgrade the catalog schema and set config. "
                    "--show only prints the config (read-only).",
        epilog="other config keys (set with --set KEY=VALUE):\n" + "\n".join(
            f"  {k} (default: {v})" for k, v in odb.CONFIG_DEFAULTS.items()
            if k not in _INIT_CONFIG_FLAGS.values()))
    pdb.add_argument("--name", help="catalog display name (GUI title / CLI banner)")
    pdb.add_argument("--seqdata-root", dest="seqdata_root",
                     help="default root of raw_sequence_data for this catalog")
    pdb.add_argument("--metadata-root", dest="metadata_root",
                     help="default dir of per-project '<project>_mapfile.csv' files")
    pdb.add_argument("--set", action="append", metavar="KEY=VALUE",
                     help="set any config key (repeatable; keys listed below)")
    pdb.add_argument("--show", action="store_true",
                     help="print the resolved config and exit (read-only; ignores other flags)")
    pdb.set_defaults(func=cmd_init_db)

    pi = sub.add_parser(
        "ingest", parents=[dbp],
        help="auto-discover projects from a seqdata + metadata dir; "
             "auto-run taxonomy, refresh data-files check (integrity is a separate step)",
        description="Ingest metadata, resolve new taxa, refresh the data-files check. "
                    "Exits 1 if any project FAILs, 2 if a prune is refused.")
    pi.add_argument("--metadata-root",
                    help="dir holding the per-project '<project>_mapfile.csv' files")
    pi.add_argument("--seqdata-root",
                    help="root of raw_sequence_data; each top-level folder is a project")
    pi.add_argument("--taxdir", help="taxdump dir (default: <db dir>/.taxonomy)")
    pi.add_argument("--skip-taxonomy", action="store_true", help="skip the taxonomy step")
    pi.add_argument("--prune", action="store_true",
                    help="delete catalog samples/files the CSV no longer lists "
                         "(then re-run `validate --seqdata-root`)")
    pi.add_argument("--prune-projects", action="store_true",
                    help="delete whole catalog projects that "
                         "vanished from BOTH the seqdata and metadata dirs (cascades "
                         "to their samples/files). Refuses to run if the roots turn up "
                         "no projects, so a missing mount can't wipe the catalog.")
    pi.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt for --prune / --prune-projects")
    pi.set_defaults(func=cmd_ingest)

    pc = sub.add_parser("checksums", parents=[dbp], help="load + compare rclone md5sum output",
                        description="Load + compare rclone md5sum output from Store and "
                                    "P-drive. Exits 1 on any md5 mismatch.")
    pc.add_argument("--store", required=True, help="rclone md5sum output for Store")
    pc.add_argument("--pdrive", required=True, help="rclone md5sum output for P-drive")
    pc.add_argument("--project", help="limit to one project_id")
    pc.add_argument("--source", default="backfill", choices=["ingest", "backfill"])
    pc.add_argument("--full", action="store_true",
                    help="also reset files in projects absent from the listings")
    pc.add_argument("--force", action="store_true",
                    help="load even if a listing looks truncated")
    pc.set_defaults(func=cmd_checksums)

    pv = sub.add_parser("validate", parents=[dbp],
                        help="re-check the catalog (data-files + checksum)",
                        description="Re-check the catalog (data-files + checksum). Exits 1 "
                                    "on missing/orphan files or md5 mismatches.")
    pv.add_argument("--seqdata-root",
                    help="root of raw_sequence_data; enables the on-disk data-files check")
    pv.set_defaults(func=cmd_validate)

    pin = sub.add_parser("integrity", parents=[dbp],
                         help="gzip + FASTQ integrity check of cataloged files",
                         description="gzip + FASTQ integrity check of cataloged files. Exits "
                                     "1 on any gzip/format error, 2 if --batch qsub is "
                                     "missing or fails.")
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
    pin.add_argument("--only-unchecked", action="store_true",
                     help="with --batch, skip submitting a job for any project whose "
                          "files have all been checked before (no never-checked file); "
                          "avoids no-op jobs on the queue. Ignored with --force. Note "
                          "each job already skips unchanged already-passed files.")
    pin.add_argument("--emit-json", metavar="PATH",
                     help="(used by batch jobs) check one --project and write results to "
                          "PATH as JSON instead of writing the DB")
    pin.add_argument("--collect", metavar="DIR",
                     help="merge batch result JSON files from DIR into the catalog and "
                          "print the per-project summary")
    pin.set_defaults(func=cmd_integrity)

    pq = sub.add_parser(
        "query", parents=[dbp], help="read-only lookups (see seqledger query -h)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Read-only lookups; never changes the catalog.",
        epilog="lookups:\n"
               "  uniq-id TERM  samples with exactly this UniqID\n"
               "  search TERM   samples whose ID, taxon, or UniqID contains TERM\n"
               "  unbacked      projects with no verified P-drive backup\n"
               "  mismatches    files whose Store and P-drive md5 differ\n"
               "  summary       one row per project (samples, files, checks, owner)\n"
               "  taxa          taxa without an exact/confirmed NCBI match\n"
               "\n"
               "columns:\n"
               "  verified           1 if every file matched its P-drive copy\n"
               "  data_check_status  last files-on-disk check: ok, issues, or unchecked\n"
               "  n_mismatch         files whose Store and P-drive md5 differ\n"
               "  n_uncompared       files whose backup is not yet verified\n"
               "  owner_name         owner of the project's files on disk\n"
               "\n"
               "Output is tab-separated; \"(none)\" goes to stderr when nothing matches.")
    pq.add_argument("what", choices=["uniq-id", "search", "unbacked", "mismatches",
                                     "summary", "taxa"], help="lookup to run (listed below)")
    pq.add_argument("term", nargs="?", default="", help="search term for uniq-id / search")
    pq.set_defaults(func=cmd_query)

    pt = sub.add_parser("taxonomy", parents=[dbp],
                        help="resolve sample taxa to NCBI TaxIDs + WoRMS AphiaIDs")
    tsub = pt.add_subparsers(dest="action", required=True)
    tr = tsub.add_parser("resolve", parents=[dbp],
                         help="resolve taxa (NCBI taxdump and/or WoRMS), write review CSV")
    tr.add_argument("--source", choices=("ncbi", "worms", "both"), default="ncbi",
                    help="taxonomy source: ncbi (default, local taxdump), worms "
                         "(WoRMS REST API), or both")
    tr.add_argument("--taxdir", help="taxdump/cache dir (default: <db dir>/.taxonomy)")
    tr.add_argument("--force-download", action="store_true", help="re-download the taxdump")
    tr.add_argument("--rebuild-index", action="store_true", help="rebuild the taxdump index")
    tr.add_argument("--refresh-unconfirmed", action="store_true",
                    help="also re-resolve taxa resolved before but not yet confirmed "
                         "(default: only taxa never checked against the source)")
    tr.add_argument("--redo", action="store_true",
                    help="re-resolve every taxon; confirmed taxa are kept (their "
                         "machine result goes to the review CSV only)")
    ta = tsub.add_parser("apply", parents=[dbp], help="apply confirmed overrides from a review CSV")
    ta.add_argument("--review", required=True, help="edited review CSV")
    ta.add_argument("--source", choices=("ncbi", "worms"), default="ncbi",
                    help="which review CSV this is: ncbi (confirmed_taxid, default) "
                         "or worms (confirmed_aphia_id)")
    ta.add_argument("--taxdir", help="taxdump/cache dir (default: <db dir>/.taxonomy)")
    pt.set_defaults(func=cmd_taxonomy)

    pg = sub.add_parser("gui", parents=[dbp], help="launch Streamlit browse GUI (locally, or on lTIO via --qsub)")
    pg.add_argument("--port", type=int, default=8501,
                    help="server port (a free port is chosen automatically under --qsub)")
    pg.add_argument("--login-host", default=None,
                    help="Hydra login host for the SSH tunnel (default: catalog config)")
    pg.add_argument("--qsub", action="store_true",
                    help="run the GUI as a job on the I/O queue (lTIO.sq) so it reads the "
                         "master catalog on Store directly (no Scratch copy); waits for it "
                         "to start, then prints the tunnel command to screen")
    pg.add_argument("--queue", default=None,
                    help="queue for --qsub (default: catalog config, else "
                         f"{odb.CONFIG_DEFAULTS['io_queue']})")
    pg.add_argument("--conda-env", default=None,
                    help="conda env activated inside the --qsub job (default: catalog config)")
    pg.add_argument("--mem", type=int, default=6, help="memory GB for the --qsub job (default 6)")
    pg.add_argument("--wait", type=int, default=300,
                    help="seconds to wait for the --qsub GUI to start serving (default 300)")
    pg.set_defaults(func=cmd_gui)

    return p


def run(argv=None):
    """Like main, but returns (rc, args) so callers can read what the command set."""
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            from seqledger import interactive
            return interactive.run(args.db), args
        parser.error("choose a command (or run 'seqledger' in a terminal for a menu)")
    if args.cmd != "init-db" or args.show:
        _require_db(args.db, any(a == "--db" or a.startswith("--db=") for a in argv))
    try:
        return args.func(args) or 0, args
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as e:
        if getattr(args, "debug", False):
            raise
        # A plain one-line message instead of a raw traceback for the common
        # mistakes (missing path/DB, bad input). Use --debug to see the traceback.
        sys.exit(f"error: {e}")


def main(argv=None):
    return run(argv)[0]

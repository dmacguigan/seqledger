"""Text menu shown when `seqledger` runs with no subcommand in a terminal.

Every action builds the same argv a user would type and runs it through
cli.run, so the printed "Same thing without the menu" line is exactly what ran.
"""

import os
import shlex
import shutil
import sqlite3
import sys

from seqledger import cli
from seqledger import db as odb
from seqledger import taxonomy as otax

MENU = """
  Look things up
    1  Search samples (sample ID, species, or UniqID)
    2  Project overview
    3  Open the browse GUI

  Data manager tasks
    4  Add or update projects from the mapfiles (ingest)
    5  Check files for damage (integrity)
    6  Verify backups from md5 listings (checksums)
    7  Look up species names (taxonomy)
"""
TAIL = """    c  Use a different catalog, or create one
    q  Quit"""
OVERVIEW = """
    1  Overview of every project
    2  Samples with an exact UniqID
    3  Projects not backed up
    4  Files whose backup copies differ
    5  Species names needing review"""
DOWNLOAD = ("Look up new species names now? First time downloads about 72 MB and "
            "uses about 1.4 GB next to the catalog.")
RCLONE = """Make the two md5 listings first, for example:
  rclone md5sum SI-Hydra:/store/nmnh_ocean_dna/public/raw_sequence_data > store.md5
  rclone md5sum /Volumes/nmnh-ocean-dna/Hydra_backup/store/raw_sequence_data > pdrive.md5
"""


def _abs(p):
    return os.path.abspath(os.path.expanduser(p))


def _ask(prompt, default=None):
    ans = input(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
    return ans or default or ""


def _yes(prompt, default=False):
    ans = input(f"{prompt} {'[Y/n]' if default else '[y/N]'} ").strip().lower()
    return ans.startswith("y") if ans else default


def _cfg(db, key):
    conn = odb.connect_ro(db)
    try:
        return odb.get_config(conn, key)
    finally:
        conn.close()


def _taxdump_ok(db):
    return os.path.exists(otax._index_path(cli._default_taxdir(db)))


def _header(db):
    """Print the header line; False when there is no catalog at db."""
    print()
    if not os.path.exists(db):
        print(f"seqledger  -  catalog: {db}  (not found)")
        print("No catalog at that path. Choose c to pick another one or create one.")
        return False
    try:
        conn = odb.connect_ro(db)
        n_p = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        n_s = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        conn.close()
        counts = f"{n_p:,} projects, {n_s:,} samples"
    except sqlite3.Error as e:
        counts = f"unreadable: {e}"
    print(f"seqledger  -  catalog: {db}  ({counts})")
    return True


def _run(db, argv):
    """Echo and run one command; returns its args Namespace, or None if it exited."""
    argv = ["--db", db] + argv
    print("\nSame thing without the menu:\n  " + shlex.join(["seqledger"] + argv) + "\n")
    try:
        return cli.run(argv)[1]
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
    except KeyboardInterrupt:
        print("\ncancelled")


def _search(db):
    term = _ask("Search for")
    if term:
        _run(db, ["query", "search", term])


def _overview(db):
    print(OVERVIEW)
    pick = _ask("Choose", "1")
    if pick == "2":
        term = _ask("UniqID")
        if term:
            _run(db, ["query", "uniq-id", term])
        return
    what = {"1": "summary", "3": "unbacked", "4": "mismatches", "5": "taxa"}.get(pick)
    if what:
        _run(db, ["query", what])


def _gui(db):
    if shutil.which("qsub"):
        _run(db, ["gui", "--qsub"])
    else:
        print("Press Ctrl-C to stop the GUI and return to the menu.")
        _run(db, ["gui"])


def _ingest(db):
    flags = []
    for flag, key, label in (("--seqdata-root", "seqdata_root", "Sequence data folder"),
                             ("--metadata-root", "metadata_root", "Mapfile folder")):
        cur = _cfg(db, key)
        ans = _ask(label, cur)
        if ans and ans != cur:
            flags += [flag, _abs(ans)]
    if not _taxdump_ok(db) and not _yes(DOWNLOAD):
        flags.append("--skip-taxonomy")
    args = _run(db, ["ingest"] + flags)
    kept, gone = getattr(args, "prunable", (0, 0))
    if not (kept or gone):
        return
    ans = _ask(f"Delete the {kept} samples and {gone} projects listed above as no longer "
               "in the mapfiles or on disk? Type DELETE to confirm")
    if ans == "DELETE":
        _run(db, ["ingest", "--prune", "--prune-projects", "--yes"] + flags)
    else:
        print("Nothing deleted.")


def _integrity(db):
    res = os.path.abspath(os.path.join("integrity_batch", "results"))
    if os.path.isdir(res) and _yes("Merge finished batch results first?", True):
        _run(db, ["integrity", "--collect", res])
    where = _ask("Run on this node or submit to the I/O queue? (q/n)",
                 "q" if shutil.which("qsub") else "n")
    if where.lower().startswith("q"):
        _run(db, ["integrity", "--batch", "--only-unchecked"])
    else:
        _run(db, ["integrity"])


def _checksums(db):
    print(RCLONE)
    paths = []
    for label in ("Store listing file", "P-drive listing file"):
        for _ in range(2):
            p = _abs(_ask(label) or ".")
            if os.path.isfile(p):
                paths.append(p)
                break
            print(f"No file at {p}")
        else:
            return
    argv = ["checksums", "--store", paths[0], "--pdrive", paths[1]]
    project = _ask("Only one project?", "all")
    if project != "all":
        argv += ["--project", project]
    _run(db, argv)


def _taxonomy(db):
    if not _taxdump_ok(db) and not _yes(DOWNLOAD):
        return
    _run(db, ["taxonomy", "resolve"])
    print("\nApplying names is not in the menu: edit confirmed_taxid in the review CSV "
          "first, then run the apply command printed above.")


def _catalog(db):
    new = _abs(_ask("Catalog path", db))
    if not os.path.exists(new):
        if not _yes("No catalog there. Create a new one?"):
            print("Ask your data manager for the catalog path.")
            return db
        argv = ["init-db"]
        for flag, label in (("--name", "Catalog name"),
                            ("--seqdata-root", "Sequence data folder"),
                            ("--metadata-root", "Mapfile folder")):
            ans = _ask(label)
            if ans:
                argv += [flag, ans if flag == "--name" else _abs(ans)]
        _run(new, argv)
        if not os.path.exists(new):
            return db
    if new != db:
        print(f"Tip: add to ~/.bashrc: export SEQLEDGER_DB={shlex.quote(new)}")
    return new


ACTIONS = {"1": _search, "2": _overview, "3": _gui, "4": _ingest,
           "5": _integrity, "6": _checksums, "7": _taxonomy}


def run(db):
    db = _abs(db)
    while True:
        ok = _header(db)
        print((MENU if ok else "") + "\n" + TAIL)
        try:
            choice = input("Choose: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice == "q":
            return 0
        if choice != "c" and not (ok and choice in ACTIONS):
            continue
        try:
            if choice == "c":
                db = _catalog(db)
            else:
                ACTIONS[choice](db)
            input("\nPress Enter to return to the menu")
        except KeyboardInterrupt:
            print("\ncancelled")
        except EOFError:
            print()
            return 0

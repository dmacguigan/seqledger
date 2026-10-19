"""Append-only TSV log of catalog issues dismissed in the GUI.

Hidden file next to the catalog; the last line for each key wins (dismiss or undo).
"""

import datetime
import os

HEADER = "time\tuser\taction\tscope\tproject_id\titem\tkind\tnote"


def default_log_path(db_path):
    db_path = os.path.abspath(db_path)
    stem = os.path.splitext(os.path.basename(db_path))[0]
    return os.path.join(os.path.dirname(db_path), "." + stem + "_dismissed.tsv")


def flatten(text):
    if text is None:
        return ""
    return str(text).replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def project_key(project_id, metadata_status, metadata_detail):
    return ("project", project_id, "", "mapfile:" + metadata_status + ":" + flatten(metadata_detail))


def sample_key(project_id, sample_id, flags):
    return ("sample", project_id, sample_id, "flags:" + flatten(flags))


def file_key(project_id, rel_path, kind):
    return ("file", project_id, rel_path, kind)


def append_many(path, action, keys, user, note=""):
    if action not in ("dismiss", "undo"):
        raise ValueError("action must be dismiss or undo, got %r" % (action,))
    time = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = "".join("\t".join([time, flatten(user), action, scope, project_id, item, kind,
                              flatten(note)]) + "\n"
                   for scope, project_id, item, kind in keys)
    new = not os.path.exists(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o664)
    try:
        if new:
            os.chmod(path, 0o664)
        if os.fstat(fd).st_size == 0:
            rows = HEADER + "\n" + rows
        os.write(fd, rows.encode("utf-8"))
    finally:
        os.close(fd)


def append(path, action, key, user, note=""):
    append_many(path, action, [key], user, note)


def read(path):
    dismissed = {}
    n_bad = 0
    try:
        f = open(path, encoding="utf-8", newline="")
    except FileNotFoundError:
        return {}, 0
    with f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line.strip() or line == HEADER:
                continue
            cols = line.split("\t")
            if len(cols) != 8:
                n_bad += 1
                continue
            time, user, action, scope, project_id, item, kind, note = cols
            key = (scope, project_id, item, kind)
            if action == "dismiss":
                dismissed[key] = {"time": time, "user": user, "note": note}
            elif action == "undo":
                dismissed.pop(key, None)
            else:
                n_bad += 1
    return dismissed, n_bad

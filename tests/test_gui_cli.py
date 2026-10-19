"""Tests for cli / gui helpers: SEQLEDGER_DB fallback, prune guard, free-port picker."""

import shutil
import socket
import stat
import subprocess

import pytest

from seqledger import cli
from seqledger import gui


def _parse(argv):
    return cli.build_parser().parse_args(argv)


# --- #35: --db falls back to $SEQLEDGER_DB ---

def test_db_defaults_to_seqledger_db_env(monkeypatch):
    monkeypatch.setenv("SEQLEDGER_DB", "/data/master.db")
    args = _parse(["query", "summary"])
    assert args.db == "/data/master.db"


def test_db_flag_overrides_env(monkeypatch):
    monkeypatch.setenv("SEQLEDGER_DB", "/data/master.db")
    args = _parse(["--db", "/tmp/other.db", "query", "summary"])
    assert args.db == "/tmp/other.db"


def test_db_falls_back_to_catalog_db_when_env_unset(monkeypatch):
    monkeypatch.delenv("SEQLEDGER_DB", raising=False)
    args = _parse(["query", "summary"])
    assert args.db == "catalog.db"


def test_db_ignores_empty_env(monkeypatch):
    monkeypatch.setenv("SEQLEDGER_DB", "")
    args = _parse(["query", "summary"])
    assert args.db == "catalog.db"


# --- #2: a non-interactive --prune / --prune-projects run is refused, not silently run ---

def test_prune_guard_refuses_non_tty():
    msg = cli._prune_guard(prune=True, prune_projects=False, yes=False, isatty=False)
    assert msg is not None
    assert "refusing" in msg.lower()
    assert "--prune" in msg


def test_prune_guard_refuses_non_tty_prune_projects():
    msg = cli._prune_guard(prune=False, prune_projects=True, yes=False, isatty=False)
    assert msg is not None
    assert "--prune-projects" in msg


def test_prune_guard_yes_bypasses_even_non_tty():
    assert cli._prune_guard(True, True, yes=True, isatty=False) is None


def test_prune_guard_no_flags_is_noop():
    assert cli._prune_guard(False, False, yes=False, isatty=False) is None


def test_prune_guard_tty_confirm_proceeds():
    assert cli._prune_guard(True, False, yes=False, isatty=True, ask=lambda _: "yes") is None


def test_prune_guard_tty_decline_aborts():
    assert cli._prune_guard(True, False, yes=False, isatty=True, ask=lambda _: "n") == "aborted."


# --- #40: free-port picker used so the printed tunnel port matches what binds ---

def test_free_port_returns_bindable_port():
    port = gui._free_port()
    assert isinstance(port, int) and port > 0
    s = socket.socket()
    try:
        s.bind(("", port))  # just-released port should still be bindable
    finally:
        s.close()


def test_free_port_prefers_requested_when_free():
    probe = socket.socket()
    probe.bind(("", 0))
    wanted = probe.getsockname()[1]
    probe.close()
    assert gui._free_port(wanted) == wanted


def test_free_port_falls_back_when_preferred_busy():
    held = socket.socket()
    held.bind(("", 0))
    busy = held.getsockname()[1]
    try:
        got = gui._free_port(busy)
        assert got != busy
        assert isinstance(got, int) and got > 0
    finally:
        held.close()


# --- loopback bind + tunnel text ---

def test_tunnel_text_qsub_uses_proxyjump():
    txt = gui._tunnel_text("compute-64-16", 41237, "hydra-login01.si.edu", "me", True)
    assert "ssh -N -L 41237:127.0.0.1:41237 -J me@hydra-login01.si.edu me@compute-64-16" in txt
    assert "http://localhost:41237" in txt


def test_tunnel_text_on_login_node_is_direct():
    txt = gui._tunnel_text("hydra-login01", 8501, "hydra-login01.si.edu", "me", True)
    assert "ssh -N -L 8501:127.0.0.1:8501 me@hydra-login01.si.edu" in txt
    assert "-J" not in txt


def test_tunnel_text_local_only_link():
    txt = gui._tunnel_text("laptop", 8501, "hydra-login01.si.edu", "me", False)
    assert "http://localhost:8501" in txt
    assert "ssh" not in txt


def _fake_launch(monkeypatch, qsub_path, host):
    runs = []
    monkeypatch.setattr(gui.shutil, "which", lambda name: qsub_path)
    monkeypatch.setattr(gui.socket, "gethostname", lambda: host)
    monkeypatch.setattr(gui.subprocess, "run", lambda cmd, **k: runs.append(cmd))
    gui.launch("x.db", port=0, login_host="hydra-login01.si.edu")
    return runs[0]


def test_launch_off_cluster_prints_only_localhost(monkeypatch, capsys):
    cmd = _fake_launch(monkeypatch, None, "my-laptop")
    out = capsys.readouterr().out
    assert "http://localhost:" in out and "ssh" not in out
    assert cmd[cmd.index("--server.address") + 1] == "127.0.0.1"


def test_launch_on_cluster_prints_tunnel(monkeypatch, capsys):
    _fake_launch(monkeypatch, "/opt/sge/bin/qsub", "compute-1-2")
    out = capsys.readouterr().out
    assert "-J " in out and "@compute-1-2" in out


def test_job_script_binds_loopback():
    s = gui._gui_job_script("/l", "/r", "q", "env", 2, "/db", 8501)
    assert "--server.address 127.0.0.1" in s
    assert "0.0.0.0" not in s


# --- READY only after the port probe succeeds ---

def _run_job_script(tmp_path, probe_rc):
    binp = tmp_path / "bin"
    binp.mkdir()
    stubs = {
        "python": '#!/bin/sh\ncase "$2" in *getsockname*) echo 5555;; *) exit %d;; esac\n' % probe_rc,
        "streamlit": "#!/bin/sh\nexit 0\n",
        "conda": "#!/bin/sh\nexit 0\n",
        "seq": "#!/bin/sh\necho 1; echo 2\n",
        "sleep": "#!/bin/sh\nexit 0\n",
    }
    for name, body in stubs.items():
        f = binp / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    (tmp_path / ".bashrc").write_text("")
    ready = tmp_path / "gui.ready"
    script = tmp_path / "gui.job"
    script.write_text(gui._gui_job_script(str(tmp_path / "gui.log"), str(ready), "q",
                                          "env", 2, "/db", 8501))
    env = {"HOME": str(tmp_path), "PATH": f"{binp}:/usr/bin:/bin"}
    rc = subprocess.run(["bash", str(script)], env=env, cwd=tmp_path).returncode
    return rc, ready.read_text().split()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_job_script_ready_after_probe_ok(tmp_path):
    rc, parts = _run_job_script(tmp_path, 0)
    assert rc == 0
    assert parts[0] == "READY" and parts[2] == "5555"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_job_script_failed_when_probe_never_ok(tmp_path):
    rc, parts = _run_job_script(tmp_path, 1)
    assert rc != 0
    assert parts == ["FAILED"]


def test_launch_qsub_exits_with_log_on_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(gui.time, "sleep", lambda *a: None)

    def fake_run(cmd, **k):
        if cmd[0] == "qsub":
            with open(cmd[1][:-4] + ".ready", "w") as fh:
                fh.write("FAILED\n")
            return subprocess.CompletedProcess(cmd, 0, "123\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(gui.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as ei:
        gui.launch_qsub("x.db", wait=30)
    assert ".seqledger_gui" in str(ei.value.code) and ".log" in str(ei.value.code)


def test_launch_qsub_prunes_old_rundir_files(monkeypatch, tmp_path):
    import os
    import time
    monkeypatch.setenv("HOME", str(tmp_path))
    rundir = tmp_path / ".seqledger_gui"
    rundir.mkdir()
    old = time.time() - 8 * 86400
    for name in ("gui_1_1.job", "gui_1_1.log", "gui_1_1.ready", "keep.txt"):
        (rundir / name).write_text("x")
        os.utime(rundir / name, (old, old))
    (rundir / "gui_2_2.log").write_text("x")

    def fake_run(cmd, **k):
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    monkeypatch.setattr(gui.subprocess, "run", fake_run)
    with pytest.raises(SystemExit):
        gui.launch_qsub("x.db", wait=1)
    names = set(os.listdir(rundir))
    assert not names & {"gui_1_1.job", "gui_1_1.log", "gui_1_1.ready"}
    assert {"keep.txt", "gui_2_2.log"} <= names


def test_gui_mem_default_is_6():
    assert cli.build_parser().parse_args(["gui"]).mem == 6

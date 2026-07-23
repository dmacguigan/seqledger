"""Launch the Streamlit browse GUI + print the SSH tunnel command.

Two modes:
  local (default)  run Streamlit on the current node (a login or interactive node).
  --qsub           submit Streamlit as a job on Hydra's I/O queue (lTIO.sq), which
                   is the only place a compute node can read the master catalog on
                   Store directly -- so no Scratch copy is needed. This waits for the
                   job to actually start serving, then prints the ready-to-run SSH
                   tunnel command to the screen (no digging through the job log).
"""

import os
import shlex
import socket
import subprocess
import sys
import time

APP_PATH = os.path.join(os.path.dirname(__file__), "app", "streamlit_app.py")
LOGIN_HOST = "hydra-login01.si.edu"


def _print_tunnel(node, port, login_host):
    user = os.environ.get("USER", "YOUR_USER_ID")
    print("\nOn your LOCAL computer, run:\n")
    print(f"  ssh -N -L {port}:{node}:{port} {user}@{login_host}\n")
    print(f"Then open http://localhost:{port} in your browser.\n")


def _free_port(preferred=None):
    """A TCP port we've just confirmed is bindable.

    Uses `preferred` when it's free, else asks the OS for any open port (bind to
    port 0). Closing the probe socket releases the port for Streamlit to claim;
    the tiny race window (port taken between here and Streamlit's bind) is
    unavoidable but far less likely than blindly reusing a default that may be busy.
    """
    for want in ([preferred] if preferred else []) + [0]:
        s = socket.socket()
        try:
            s.bind(("", want))
            return s.getsockname()[1]
        except OSError:
            continue
        finally:
            s.close()


def launch(db_path, port=8501, login_host=LOGIN_HOST):
    """Run Streamlit headless on THIS node and print the tunnel command to reach it."""
    # Resolve to a port we've confirmed is free BEFORE printing the tunnel, so the
    # localhost:PORT we print matches what Streamlit actually binds (otherwise, if
    # the requested port were busy, Streamlit would silently pick another and the
    # printed tunnel would point at the wrong port).
    port = _free_port(port)
    _print_tunnel(socket.gethostname(), port, login_host)
    env = dict(os.environ, SEQLEDGER_DB=os.path.abspath(db_path))
    # DEFERRED (bind exposure): the documented tunnel is
    #   ssh -N -L <port>:<NODE>:<port> <user>@<login-host>
    # so the login host reaches this server at <NODE>:<port> over the cluster
    # network. Binding 127.0.0.1 would make the server reachable only from this
    # node itself and BREAK that login->node hop, so we keep 0.0.0.0. Tradeoff:
    # the app is exposed on all interfaces with no auth -- rely on an HPC
    # firewall/ACL (compute nodes are not internet-reachable), or add Streamlit
    # auth, rather than switching the bind.
    cmd = [sys.executable, "-m", "streamlit", "run", APP_PATH,
           "--server.address", "0.0.0.0", "--server.port", str(port),
           "--server.headless", "true"]
    try:
        subprocess.run(cmd, env=env)
    except KeyboardInterrupt:
        print("\nGUI stopped.")


def _qstat_state(jid):
    """SGE job state for jid ('r' running, 'qw' queued, ...) or None if not listed."""
    out = subprocess.run(["qstat"], capture_output=True, text=True)
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] == jid:
            # SGE columns: job-ID prior name user state submit/start-at queue ...
            return parts[4] if len(parts) > 4 else "?"
    return None


def _gui_job_script(log_path, ready_path, queue, conda_env, mem_gb, db_abs, port):
    """A qsub script that serves the GUI on the I/O queue and signals readiness.

    Streamlit is started in the background; the script waits until the port is
    actually listening before writing '<host> <port>' to ready_path, so the tunnel
    we print always points at a live server. SEQLEDGER_DB points at the master DB
    on Store, which is reachable from lTIO -- no Scratch copy involved.
    """
    return f"""#!/bin/bash
#$ -N seqledger_gui
#$ -o {log_path}
#$ -j y
#$ -terse
#$ -notify
#$ -q {queue} -l ioq
#$ -l mres={mem_gb}G,h_data={mem_gb}G,h_vmem={mem_gb}G
#$ -S /bin/bash
#$ -cwd

echo + `date` $JOB_NAME on $HOSTNAME in $QUEUE jobID=$JOB_ID
source ~/.bashrc
conda activate {conda_env}
export SEQLEDGER_DB={shlex.quote(db_abs)}

# Pick a free port on this node (fall back to the requested one).
PORT=$(python -c 'import socket;s=socket.socket();s.bind(("",0));print(s.getsockname()[1]);s.close()' 2>/dev/null || echo {port})

# DEFERRED (bind exposure): the tunnel is `ssh -L <port>:<NODE>:<port> ...`, so the
# login host reaches this GUI at <NODE>:<port> over the cluster network. Binding
# 127.0.0.1 here would make it reachable only from this compute node and BREAK the
# login->node hop, so we keep 0.0.0.0. Tradeoff: no-auth exposure on all interfaces
# -- mitigate with the HPC firewall/ACL (compute nodes aren't internet-reachable)
# or Streamlit auth, not by changing the bind.
streamlit run {shlex.quote(APP_PATH)} --server.address 0.0.0.0 --server.port "$PORT" --server.headless true &
SPID=$!

# Wait (up to 90s) for the server to actually accept connections, then signal ready.
for i in $(seq 1 90); do
    if python -c "import socket,sys; sys.exit(0 if socket.socket().connect_ex(('127.0.0.1', $PORT))==0 else 1)"; then
        break
    fi
    sleep 1
done
echo "READY $(hostname) $PORT" > {shlex.quote(ready_path)}
wait $SPID
"""


def launch_qsub(db_path, port=8501, login_host=LOGIN_HOST, queue="lTIO.sq",
                conda_env="seqledger", mem_gb=2, wait=300):
    """Submit the GUI as a job on the I/O queue, wait for it to serve, print the tunnel.

    Runs on a Hydra login node. Returns after printing the tunnel; the GUI keeps
    running on the queue until you `qdel` it (or the lTIO 72h wall cap ends it).
    """
    db_abs = os.path.abspath(db_path)
    rundir = os.path.join(os.path.expanduser("~"), ".seqledger_gui")
    os.makedirs(rundir, exist_ok=True)
    stamp = f"{os.getpid()}_{int(time.time())}"
    ready = os.path.join(rundir, f"gui_{stamp}.ready")
    log_path = os.path.join(rundir, f"gui_{stamp}.log")
    script = os.path.join(rundir, f"gui_{stamp}.job")
    if os.path.exists(ready):
        os.remove(ready)

    with open(script, "w") as f:
        f.write(_gui_job_script(log_path, ready, queue, conda_env, mem_gb, db_abs, port))

    try:
        out = subprocess.run(["qsub", script], capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("qsub not found -- run `gui --qsub` on a Hydra login node, or drop "
                 "--qsub to run the GUI on the current node.")
    if out.returncode != 0:
        sys.exit(f"qsub failed: {out.stderr.strip() or out.stdout.strip()}")
    jid = out.stdout.strip()

    print(f"submitted GUI job {jid} to {queue} (reads the master catalog on Store "
          "directly -- no Scratch copy).")
    print(f"waiting up to {wait}s for it to start serving ...", flush=True)

    node = srvport = None
    start = time.monotonic()
    last_beat = 0.0
    while time.monotonic() - start < wait:
        if os.path.exists(ready):
            with open(ready) as fh:
                parts = fh.read().split()
            if len(parts) >= 3 and parts[0] == "READY":
                node, srvport = parts[1], parts[2]
                break
        now = time.monotonic()
        if now - last_beat >= 15:
            state = _qstat_state(jid)
            if state is None and not os.path.exists(ready):
                print(f"  job {jid} is no longer queued/running -- it may have failed. "
                      f"Check the log:\n    {log_path}")
                sys.exit(1)
            label = {"qw": "queued (lTIO busy -- max 2 jobs/user)",
                     "r": "running, starting Streamlit"}
            print(f"  [{int(now - start)}s] job {jid}: {label.get(state, state)}", flush=True)
            last_beat = now
        time.sleep(3)

    if not node:
        sys.exit(f"\nGUI job {jid} did not report ready within {wait}s.\n"
                 f"  check state: qstat -j {jid}\n"
                 f"  check log:   {log_path}\n"
                 f"  stop it:     qdel {jid}")

    print(f"\nGUI is serving on {queue} node {node}:{srvport} with direct Store access.")
    _print_tunnel(node, srvport, login_host)
    print(f"Job {jid}  ·  log: {log_path}")
    print(f"Stop the GUI when you're done:  qdel {jid}")
    print("Note: lTIO caps jobs at 72h wall and 2 concurrent jobs/user -- the GUI "
          "stops at 72h; just resubmit to restart.")

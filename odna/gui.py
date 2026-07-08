"""Launch the Streamlit browse GUI, printing the SSH tunnel command (MitoPilot style)."""

import os
import socket
import subprocess
import sys

APP_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "streamlit_app.py")


def launch(db_path, port=8501, login_host="hydra-login01.si.edu"):
    """Run Streamlit headless and print the tunnel command to reach it."""
    node = socket.gethostname()
    user = os.environ.get("USER", "YOUR_USER_ID")
    print("\nMitoPilot-style SSH tunnel. On your LOCAL computer, run:\n")
    print(f"  ssh -N -L {port}:{node}:{port} {user}@{login_host}\n")
    print(f"Then open http://localhost:{port} in your browser.\n")

    env = dict(os.environ, ODNA_DB=os.path.abspath(db_path))
    cmd = [
        sys.executable, "-m", "streamlit", "run", APP_PATH,
        "--server.address", "0.0.0.0",
        "--server.port", str(port),
        "--server.headless", "true",
    ]
    subprocess.run(cmd, env=env)

"""Tests for cli / gui helpers: SEQLEDGER_DB fallback, prune guard, free-port picker."""

import socket

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

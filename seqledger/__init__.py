"""Ocean DNA raw sequence data catalog tooling."""
from importlib.metadata import PackageNotFoundError, version
try:
    __version__ = version("seqledger")
except PackageNotFoundError:
    __version__ = "0.1.0-dev"

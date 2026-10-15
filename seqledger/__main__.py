"""Enable `python -m seqledger`."""

import sys

from seqledger.cli import main

if __name__ == "__main__":
    sys.exit(main())

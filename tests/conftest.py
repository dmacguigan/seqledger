import os
import sys

# Put the data_management_db root on the path so `import seqledger` finds the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

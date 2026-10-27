import sys
from pathlib import Path

# make tests/fake_plugin.py importable as a plain module
sys.path.insert(0, str(Path(__file__).resolve().parent))

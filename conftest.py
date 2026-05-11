"""Pytest config: make the repo root importable so `demo.synthetic_run` works."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

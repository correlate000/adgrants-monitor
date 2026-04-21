"""Shared pytest configuration.

Makes the repo root importable so `import ads_common.*` and script-level
imports work when tests are run from anywhere (CI, editor, etc.).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

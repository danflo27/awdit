from __future__ import annotations

import sys
from pathlib import Path


# Allow `pytest` to import the package directly from an uninstalled src-layout checkout.
SRC_PATH = Path(__file__).resolve().parents[1] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

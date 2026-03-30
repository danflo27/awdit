from __future__ import annotations

from pathlib import Path


MANAGED_ROOT_NAME = "awdit"
MANAGED_DATA_DIR_NAME = "data"


def managed_root(cwd: Path) -> Path:
    return cwd / MANAGED_ROOT_NAME / MANAGED_DATA_DIR_NAME

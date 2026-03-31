from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


RUNS_ROOT_NAME = "runs"
REPOS_ROOT_NAME = "repos"
WORKTREES_ROOT_NAME = "worktrees"
STATE_ROOT_NAME = "state"

LEGACY_MANAGED_ROOT_NAME = "awdit"
LEGACY_MANAGED_DATA_DIR_NAME = "data"


@dataclass(frozen=True)
class LegacyMigrationResult:
    moved: tuple[str, ...]
    skipped: tuple[str, ...]


def runs_root(cwd: Path) -> Path:
    return cwd / RUNS_ROOT_NAME


def repos_root(cwd: Path) -> Path:
    return cwd / REPOS_ROOT_NAME


def worktrees_root(cwd: Path) -> Path:
    return cwd / WORKTREES_ROOT_NAME


def state_root(cwd: Path) -> Path:
    return cwd / STATE_ROOT_NAME


def managed_runtime_root_names(*, include_legacy: bool = True) -> tuple[str, ...]:
    names = [RUNS_ROOT_NAME, REPOS_ROOT_NAME, WORKTREES_ROOT_NAME, STATE_ROOT_NAME]
    if include_legacy:
        names.append(LEGACY_MANAGED_ROOT_NAME)
    return tuple(names)


def migrate_legacy_runtime_layout(cwd: Path) -> LegacyMigrationResult:
    cwd = cwd.resolve()
    moved: list[str] = []
    skipped: list[str] = []

    legacy_root = cwd / LEGACY_MANAGED_ROOT_NAME
    legacy_data_root = legacy_root / LEGACY_MANAGED_DATA_DIR_NAME

    _merge_children(
        source_dir=legacy_data_root / "runs",
        destination_dir=runs_root(cwd),
        moved=moved,
        skipped=skipped,
        label="runs",
    )
    _merge_children(
        source_dir=legacy_root / "runs",
        destination_dir=runs_root(cwd),
        moved=moved,
        skipped=skipped,
        label="runs",
    )
    _merge_children(
        source_dir=legacy_root / "repos",
        destination_dir=repos_root(cwd),
        moved=moved,
        skipped=skipped,
        label="repos",
    )
    _merge_children(
        source_dir=legacy_root / "worktrees",
        destination_dir=worktrees_root(cwd),
        moved=moved,
        skipped=skipped,
        label="worktrees",
    )

    legacy_db = legacy_root / "awdit.db"
    target_db = state_root(cwd) / "awdit.db"
    if legacy_db.exists():
        target_db.parent.mkdir(parents=True, exist_ok=True)
        if target_db.exists():
            skipped.append(f"state/awdit.db (destination already exists)")
        else:
            shutil.move(str(legacy_db), str(target_db))
            moved.append(f"{legacy_db.relative_to(cwd)} -> {target_db.relative_to(cwd)}")

    _prune_empty_dirs(legacy_data_root, stop_at=cwd)
    _prune_empty_dirs(legacy_root, stop_at=cwd)
    return LegacyMigrationResult(moved=tuple(moved), skipped=tuple(skipped))


def _merge_children(
    *,
    source_dir: Path,
    destination_dir: Path,
    moved: list[str],
    skipped: list[str],
    label: str,
) -> None:
    if not source_dir.exists() or not source_dir.is_dir():
        return
    destination_dir.mkdir(parents=True, exist_ok=True)
    for child in sorted(source_dir.iterdir()):
        target = destination_dir / child.name
        if target.exists():
            skipped.append(f"{label}/{child.name} (destination already exists)")
            continue
        shutil.move(str(child), str(target))
        moved.append(f"{child} -> {target}")


def _prune_empty_dirs(path: Path, *, stop_at: Path) -> None:
    current = path
    while current != stop_at and current.exists() and current.is_dir():
        try:
            next(current.iterdir())
        except StopIteration:
            current.rmdir()
            current = current.parent
            continue
        break

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


RUNS_ROOT_NAME = "runs"
REPOS_ROOT_NAME = "repos"
WORKTREES_ROOT_NAME = "worktrees"
STATE_ROOT_NAME = "state"

AWDIT_DATA_ROOT_ENV = "AWDIT_DATA_ROOT"

LEGACY_MANAGED_ROOT_NAME = "awdit"
LEGACY_MANAGED_DATA_DIR_NAME = "data"


@dataclass(frozen=True)
class LegacyMigrationResult:
    moved: tuple[str, ...]
    skipped: tuple[str, ...]


def default_data_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_data_root(*, env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    configured = str(environ.get(AWDIT_DATA_ROOT_ENV, "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return default_data_root().resolve()


def runs_root(cwd: Path, *, data_root: Path | None = None) -> Path:
    return _runtime_storage_root(data_root) / RUNS_ROOT_NAME


def repos_root(cwd: Path, *, data_root: Path | None = None) -> Path:
    return _runtime_storage_root(data_root) / REPOS_ROOT_NAME


def worktrees_root(cwd: Path, *, data_root: Path | None = None) -> Path:
    return _runtime_storage_root(data_root) / WORKTREES_ROOT_NAME


def state_root(cwd: Path, *, data_root: Path | None = None) -> Path:
    return _runtime_storage_root(data_root) / STATE_ROOT_NAME


def infer_managed_data_root(path: Path, *, include_legacy: bool = True) -> Path | None:
    resolved = path.resolve()
    for index, part in enumerate(resolved.parts):
        if part not in managed_runtime_root_names(include_legacy=include_legacy):
            continue
        prefix_parts = resolved.parts[:index]
        if not prefix_parts:
            return Path(resolved.anchor)
        return Path(*prefix_parts)
    return None


def managed_runtime_root_names(*, include_legacy: bool = True) -> tuple[str, ...]:
    names = [RUNS_ROOT_NAME, REPOS_ROOT_NAME, WORKTREES_ROOT_NAME, STATE_ROOT_NAME]
    if include_legacy:
        names.append(LEGACY_MANAGED_ROOT_NAME)
    return tuple(names)


def migrate_legacy_runtime_layout(cwd: Path, *, data_root: Path | None = None) -> LegacyMigrationResult:
    cwd = cwd.resolve()
    storage_root = _runtime_storage_root(data_root)
    moved: list[str] = []
    skipped: list[str] = []

    legacy_root = cwd / LEGACY_MANAGED_ROOT_NAME
    legacy_data_root = legacy_root / LEGACY_MANAGED_DATA_DIR_NAME
    runs_dir = runs_root(cwd, data_root=storage_root)
    repos_dir = repos_root(cwd, data_root=storage_root)
    worktrees_dir = worktrees_root(cwd, data_root=storage_root)
    state_dir = state_root(cwd, data_root=storage_root)

    _merge_children(
        source_dir=legacy_data_root / "runs",
        destination_dir=runs_dir,
        moved=moved,
        skipped=skipped,
        label="runs",
        stop_at=cwd,
    )
    _merge_children(
        source_dir=legacy_root / "runs",
        destination_dir=runs_dir,
        moved=moved,
        skipped=skipped,
        label="runs",
        stop_at=cwd,
    )
    _merge_children(
        source_dir=cwd / RUNS_ROOT_NAME,
        destination_dir=runs_dir,
        moved=moved,
        skipped=skipped,
        label="runs",
        stop_at=cwd,
    )
    _merge_children(
        source_dir=legacy_root / "repos",
        destination_dir=repos_dir,
        moved=moved,
        skipped=skipped,
        label="repos",
        stop_at=cwd,
    )
    _merge_children(
        source_dir=cwd / REPOS_ROOT_NAME,
        destination_dir=repos_dir,
        moved=moved,
        skipped=skipped,
        label="repos",
        stop_at=cwd,
    )
    _merge_children(
        source_dir=legacy_root / "worktrees",
        destination_dir=worktrees_dir,
        moved=moved,
        skipped=skipped,
        label="worktrees",
        stop_at=cwd,
    )
    _merge_children(
        source_dir=cwd / WORKTREES_ROOT_NAME,
        destination_dir=worktrees_dir,
        moved=moved,
        skipped=skipped,
        label="worktrees",
        stop_at=cwd,
    )

    legacy_db = legacy_root / "awdit.db"
    source_db = cwd / STATE_ROOT_NAME / "awdit.db"
    target_db = state_dir / "awdit.db"
    _move_file(
        source_path=legacy_db,
        destination_path=target_db,
        moved=moved,
        skipped=skipped,
        label="state/awdit.db",
        display_root=cwd,
    )
    _move_file(
        source_path=source_db,
        destination_path=target_db,
        moved=moved,
        skipped=skipped,
        label="state/awdit.db",
        display_root=cwd,
    )

    _prune_empty_dirs(legacy_data_root, stop_at=cwd)
    _prune_empty_dirs(legacy_root, stop_at=cwd)
    _prune_empty_dirs(cwd / STATE_ROOT_NAME, stop_at=cwd)
    return LegacyMigrationResult(moved=tuple(moved), skipped=tuple(skipped))


def _runtime_storage_root(data_root: Path | None) -> Path:
    if data_root is None:
        return resolve_data_root()
    return data_root.expanduser().resolve()


def _merge_children(
    *,
    source_dir: Path,
    destination_dir: Path,
    moved: list[str],
    skipped: list[str],
    label: str,
    stop_at: Path,
) -> None:
    if not source_dir.exists() or not source_dir.is_dir():
        return
    if source_dir.resolve() == destination_dir.resolve():
        return
    destination_dir.mkdir(parents=True, exist_ok=True)
    for child in sorted(source_dir.iterdir()):
        target = destination_dir / child.name
        if target.exists():
            skipped.append(f"{label}/{child.name} (destination already exists)")
            continue
        shutil.move(str(child), str(target))
        moved.append(f"{_display_path(child, stop_at)} -> {_display_path(target, stop_at)}")
    _prune_empty_dirs(source_dir, stop_at=stop_at)


def _move_file(
    *,
    source_path: Path,
    destination_path: Path,
    moved: list[str],
    skipped: list[str],
    label: str,
    display_root: Path,
) -> None:
    if not source_path.exists():
        return
    if source_path.resolve() == destination_path.resolve():
        return
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.exists():
        skipped.append(f"{label} (destination already exists)")
        return
    shutil.move(str(source_path), str(destination_path))
    moved.append(f"{_display_path(source_path, display_root)} -> {_display_path(destination_path, display_root)}")


def _display_path(path: Path, display_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(display_root))
    except ValueError:
        return str(path.resolve())


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

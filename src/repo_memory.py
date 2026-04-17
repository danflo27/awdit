from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from paths import repos_root


@dataclass(frozen=True)
class RepoIdentity:
    repo_name: str
    repo_key: str
    source_kind: str
    source_value: str
    repo_dir: Path


def _repo_key_for_source(repo_name: str, source_value: str, *, digest_length: int) -> str:
    digest = hashlib.sha256(source_value.encode("utf-8")).hexdigest()[:digest_length]
    return f"{repo_name}_{digest}"


def resolve_repo_identity(cwd: Path) -> RepoIdentity:
    repo_dir = cwd.resolve()
    repo_name = repo_dir.name or "repo"

    remote_url = _git_remote_url(repo_dir)
    if remote_url:
        source_kind = "git_remote"
        source_value = remote_url
    else:
        source_kind = "repo_path"
        source_value = str(repo_dir)

    repo_key = _repo_key_for_source(repo_name, source_value, digest_length=32)
    return RepoIdentity(
        repo_name=repo_name,
        repo_key=repo_key,
        source_kind=source_kind,
        source_value=source_value,
        repo_dir=repo_dir,
    )


def repo_memory_dir(cwd: Path, repo_key: str, *, data_root: Path | None = None) -> Path:
    return repos_root(cwd.resolve(), data_root=data_root) / repo_key


def legacy_repo_key(identity: RepoIdentity) -> str:
    return _repo_key_for_source(identity.repo_name, identity.source_value, digest_length=8)


def migrate_legacy_repo_memory_dir(cwd: Path, identity: RepoIdentity, *, data_root: Path | None = None) -> None:
    repo_dir = cwd.resolve()
    current_dir = repo_memory_dir(repo_dir, identity.repo_key, data_root=data_root)
    legacy_dirs = [repo_memory_dir(repo_dir, legacy_repo_key(identity), data_root=data_root)]
    repo_local_legacy_dir = repo_memory_dir(repo_dir, legacy_repo_key(identity), data_root=repo_dir)
    if repo_local_legacy_dir not in legacy_dirs:
        legacy_dirs.append(repo_local_legacy_dir)
    if current_dir.exists():
        return
    for legacy_dir in legacy_dirs:
        if not legacy_dir.exists():
            continue
        current_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_dir), str(current_dir))
        return


def danger_map_paths(cwd: Path, repo_key: str, *, data_root: Path | None = None) -> dict[str, Path]:
    base_dir = repo_memory_dir(cwd, repo_key, data_root=data_root)
    return {
        "repo_dir": base_dir,
        "danger_map_md": base_dir / "danger_map.md",
        "danger_map_json": base_dir / "danger_map.json",
        "memory_dir": base_dir / "memory",
        "repo_comments_md": base_dir / "memory" / "repo_comments.md",
    }


def _git_remote_url(repo_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo_dir,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None

    if result.returncode != 0:
        return None

    value = result.stdout.strip()
    return value or None

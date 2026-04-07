from __future__ import annotations

import hashlib
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

    digest = hashlib.sha256(source_value.encode("utf-8")).hexdigest()[:8]
    repo_key = f"{repo_name}_{digest}"
    return RepoIdentity(
        repo_name=repo_name,
        repo_key=repo_key,
        source_kind=source_kind,
        source_value=source_value,
        repo_dir=repo_dir,
    )


def repo_memory_dir(cwd: Path, repo_key: str) -> Path:
    return repos_root(cwd.resolve()) / repo_key


def danger_map_paths(cwd: Path, repo_key: str) -> dict[str, Path]:
    base_dir = repo_memory_dir(cwd, repo_key)
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

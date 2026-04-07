from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from paths import managed_runtime_root_names
from provider_openai import OpenAIResponsesProvider
from repo_memory import RepoIdentity, danger_map_paths, resolve_repo_identity


@dataclass(frozen=True)
class DangerMapResult:
    identity: RepoIdentity
    danger_map_md: Path
    danger_map_json: Path
    repo_comments_md: Path
    payload: dict[str, Any]


def load_danger_map_result(cwd: Path, repo_key: str) -> DangerMapResult | None:
    artifact_paths = danger_map_paths(cwd, repo_key)
    if not artifact_paths["danger_map_md"].exists() or not artifact_paths["danger_map_json"].exists():
        return None

    payload = _parse_json_object(artifact_paths["danger_map_json"].read_text(encoding="utf-8"))
    identity = RepoIdentity(
        repo_name=str(payload.get("repo_name", "") or Path(cwd).resolve().name or "repo"),
        repo_key=str(payload.get("repo_key", repo_key) or repo_key),
        source_kind="existing_map",
        source_value=str(cwd.resolve()),
        repo_dir=cwd.resolve(),
    )
    _ensure_repo_comments_file(artifact_paths["repo_comments_md"])
    return DangerMapResult(
        identity=identity,
        danger_map_md=artifact_paths["danger_map_md"],
        danger_map_json=artifact_paths["danger_map_json"],
        repo_comments_md=artifact_paths["repo_comments_md"],
        payload=payload,
    )


class RepoReadOnlyTools:
    def __init__(self, *, cwd: Path, scope_include: tuple[str, ...], scope_exclude: tuple[str, ...]) -> None:
        self.cwd = cwd.resolve()
        self.scope_include = scope_include
        self.scope_exclude = scope_exclude

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": "list_scope_files",
                "description": "List allowed repo files that are in scope for swarm inspection.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path_glob": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                    },
                },
            },
            {
                "type": "function",
                "name": "read_file",
                "description": "Read an allowed repo file inside the current repo scope.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "max_chars": {"type": "integer", "minimum": 1, "maximum": 50000},
                    },
                    "required": ["path"],
                },
            },
        ]

    def run(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "list_scope_files":
            path_glob = str(arguments.get("path_glob", "") or "").strip()
            limit = int(arguments.get("limit", 50) or 50)
            paths = [self._display_path(path) for path in self.allowed_paths(path_glob=path_glob)[:limit]]
            return json.dumps({"paths": paths, "count": len(paths)}, indent=2)
        if tool_name == "read_file":
            raw_path = str(arguments.get("path", "") or "").strip()
            if not raw_path:
                raise RuntimeError("read_file requires a path.")
            max_chars = int(arguments.get("max_chars", 12000) or 12000)
            path = self.resolve_allowed_path(raw_path)
            text = path.read_text(encoding="utf-8", errors="replace")
            return json.dumps(
                {
                    "path": self._display_path(path),
                    "content": text[:max_chars],
                    "truncated": len(text) > max_chars,
                },
                indent=2,
            )
        raise RuntimeError(f"Unknown tool: {tool_name}")

    def allowed_paths(self, *, path_glob: str = "") -> list[Path]:
        allowed: list[Path] = []
        for path in list_repo_files(self.cwd):
            relative = path.relative_to(self.cwd).as_posix()
            if self.scope_include and not _matches_any(relative, self.scope_include):
                continue
            if _matches_any(relative, self.scope_exclude):
                continue
            if path_glob and not PurePosixPath(relative).match(path_glob):
                continue
            allowed.append(path.resolve())
        return allowed

    def resolve_allowed_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (self.cwd / path).resolve()
        else:
            path = path.resolve()
        if not _is_relative_to(path, self.cwd):
            raise RuntimeError("Path is outside the repo root.")
        relative = path.relative_to(self.cwd).as_posix()
        if _is_runtime_managed_relative(relative):
            raise RuntimeError("Managed runtime files are not readable through swarm tools.")
        if self.scope_include and not _matches_any(relative, self.scope_include):
            raise RuntimeError("Path is outside the configured scope include globs.")
        if _matches_any(relative, self.scope_exclude):
            raise RuntimeError("Path is excluded by scope rules.")
        if not path.exists() or not path.is_file():
            raise RuntimeError("File does not exist.")
        return path

    def _display_path(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.cwd))


def generate_danger_map(
    *,
    cwd: Path,
    loaded,
    provider: OpenAIResponsesProvider,
    guidance_notes: tuple[str, ...] = (),
) -> DangerMapResult:
    swarm_config = loaded.effective.swarm
    if swarm_config is None:
        raise RuntimeError("Swarm config is not available.")

    identity = resolve_repo_identity(cwd)
    artifact_paths = danger_map_paths(cwd, identity.repo_key)
    artifact_paths["memory_dir"].mkdir(parents=True, exist_ok=True)
    _ensure_repo_comments_file(artifact_paths["repo_comments_md"])
    effective_guidance = _merge_guidance(
        read_repo_guidance(artifact_paths["repo_comments_md"]),
        guidance_notes,
    )

    tools = RepoReadOnlyTools(
        cwd=cwd,
        scope_include=loaded.effective.scope.include,
        scope_exclude=loaded.effective.scope.exclude,
    )
    input_text = build_danger_map_input(
        cwd=cwd,
        loaded=loaded,
        identity=identity,
        guidance_notes=effective_guidance,
    )
    instructions = swarm_config.prompt_file.read_text(encoding="utf-8")
    result = provider.start_foreground_turn(
        model=swarm_config.sweep_model,
        reasoning_effort="high",
        instructions=instructions,
        input_text=input_text,
        previous_response_id=None,
        tools=tools.schemas(),
        tool_executor=tools.run,
    )
    payload = _parse_json_object(result.final_text)
    normalized = normalize_danger_map_payload(
        payload=payload,
        identity=identity,
        guidance_notes=effective_guidance,
    )
    artifact_paths["danger_map_json"].write_text(
        json.dumps(normalized, indent=2) + "\n",
        encoding="utf-8",
    )
    artifact_paths["danger_map_md"].write_text(
        render_danger_map_markdown(normalized),
        encoding="utf-8",
    )
    return DangerMapResult(
        identity=identity,
        danger_map_md=artifact_paths["danger_map_md"],
        danger_map_json=artifact_paths["danger_map_json"],
        repo_comments_md=artifact_paths["repo_comments_md"],
        payload=normalized,
    )


def append_repo_guidance(repo_comments_md: Path, guidance: str) -> None:
    entry = guidance.strip()
    if not entry:
        return
    timestamp = datetime.now().isoformat(timespec="seconds")
    with repo_comments_md.open("a", encoding="utf-8") as handle:
        handle.write(f"## {timestamp}\n")
        handle.write(f"{entry}\n\n")


def read_repo_guidance(repo_comments_md: Path) -> tuple[str, ...]:
    if not repo_comments_md.exists():
        return ()

    entries: list[str] = []
    for raw_line in repo_comments_md.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line not in entries:
            entries.append(line)
    return tuple(entries)


def _ensure_repo_comments_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("# Repo comments\n\n", encoding="utf-8")


def build_danger_map_input(
    *,
    cwd: Path,
    loaded,
    identity: RepoIdentity,
    guidance_notes: tuple[str, ...],
) -> str:
    repo_dir = cwd.resolve()
    tracked_files = list_repo_files(cwd)
    inventory = "\n".join(
        f"- {path.relative_to(repo_dir).as_posix()}" for path in tracked_files[:400]
    )
    guidance_block = "\n".join(f"- {item}" for item in guidance_notes) or "- (none)"
    scope_include = ", ".join(loaded.effective.scope.include) or "(none)"
    scope_exclude = ", ".join(loaded.effective.scope.exclude) or "(none)"
    return "\n".join(
        [
            "Generate a compact repo danger map as one JSON object only.",
            "Return valid JSON and no surrounding prose.",
            "Expected keys: trust_boundaries, risky_sinks, auth_assumptions, hot_paths, notes.",
            "Each key should map to a list of concise strings.",
            f"Repo name: {identity.repo_name}",
            f"Repo key: {identity.repo_key}",
            f"Repo identity source: {identity.source_kind}",
            f"Scope include: {scope_include}",
            f"Scope exclude: {scope_exclude}",
            "User guidance:",
            guidance_block,
            "Tracked repo inventory:",
            inventory or "- (none)",
        ]
    )


def normalize_danger_map_payload(
    *,
    payload: dict[str, Any],
    identity: RepoIdentity,
    guidance_notes: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "repo_key": identity.repo_key,
        "repo_name": identity.repo_name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "guidance": [str(item).strip() for item in guidance_notes if str(item).strip()],
        "trust_boundaries": _string_list(payload.get("trust_boundaries")),
        "risky_sinks": _string_list(payload.get("risky_sinks")),
        "auth_assumptions": _string_list(payload.get("auth_assumptions")),
        "hot_paths": _string_list(payload.get("hot_paths")),
        "notes": _string_list(payload.get("notes")),
    }


def render_danger_map_markdown(payload: dict[str, Any]) -> str:
    sections = [
        ("Trust boundaries", payload["trust_boundaries"]),
        ("Risky sinks", payload["risky_sinks"]),
        ("Auth assumptions", payload["auth_assumptions"]),
        ("Hot paths", payload["hot_paths"]),
        ("Notes", payload["notes"]),
    ]
    lines = [
        "# Repo danger map",
        "",
        f"- Repo: `{payload['repo_name']}`",
        f"- Repo key: `{payload['repo_key']}`",
        f"- Generated at: `{payload['generated_at']}`",
    ]
    guidance = payload.get("guidance") or []
    if guidance:
        lines.extend(["", "## Guidance"])
        for item in guidance:
            lines.append(f"- {item}")
    for title, items in sections:
        lines.extend(["", f"## {title}"])
        if not items:
            lines.append("- (none)")
            continue
        for item in items:
            lines.append(f"- {item}")
    return "\n".join(lines).strip() + "\n"


def list_repo_files(cwd: Path) -> list[Path]:
    repo_dir = cwd.resolve()
    git_paths = _git_tracked_files(repo_dir)
    if git_paths is not None:
        return git_paths

    files: list[Path] = []
    for path in sorted(repo_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(repo_dir).as_posix()
        if _is_runtime_managed_relative(relative):
            continue
        files.append(path.resolve())
    return files


def _git_tracked_files(repo_dir: Path) -> list[Path] | None:
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_dir,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None

    if result.returncode != 0:
        return None

    files: list[Path] = []
    for line in result.stdout.splitlines():
        value = line.strip()
        if not value:
            continue
        path = (repo_dir / value).resolve()
        if path.is_file():
            files.append(path)
    return sorted(files)


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Expected JSON response from swarm model: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Expected JSON object response from swarm model.")
    return value


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            items.append(text)
    return items


def _merge_guidance(*groups: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []
    for group in groups:
        for item in group:
            value = str(item).strip()
            if value and value not in merged:
                merged.append(value)
    return tuple(merged)


def _matches_any(relative_path: str, globs: tuple[str, ...]) -> bool:
    posix = PurePosixPath(relative_path)
    return any(posix.match(pattern) for pattern in globs)


def _is_runtime_managed_relative(relative_path: str) -> bool:
    for root_name in managed_runtime_root_names(include_legacy=True):
        if relative_path == root_name or relative_path.startswith(f"{root_name}/"):
            return True
    return False


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return True

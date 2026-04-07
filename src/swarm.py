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

CODE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".cxx",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".mjs",
    ".php",
    ".ps1",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".sh",
    ".sql",
    ".swift",
    ".ts",
    ".tsx",
    ".zsh",
}
CONFIG_EXTENSIONS = {
    ".cfg",
    ".conf",
    ".ini",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
}
CONFIG_FILENAMES = {
    ".gitignore",
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "makefile",
    "justfile",
    "pyproject.toml",
    "uv.lock",
}


@dataclass(frozen=True)
class DangerMapResult:
    identity: RepoIdentity
    danger_map_md: Path
    danger_map_json: Path
    repo_comments_md: Path
    payload: dict[str, Any]


@dataclass(frozen=True)
class RepoFileEntry:
    relative_path: str
    path: Path


@dataclass(frozen=True)
class SwarmSeedResult:
    seed_id: str
    target_file: str
    outcome: str
    severity_bucket: str
    claim: str
    evidence: tuple[str, ...]
    related_files: tuple[str, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed_id": self.seed_id,
            "target_file": self.target_file,
            "outcome": self.outcome,
            "severity_bucket": self.severity_bucket,
            "claim": self.claim,
            "evidence": list(self.evidence),
            "related_files": list(self.related_files),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class SwarmSweepResult:
    seeds_dir: Path
    proofs_dir: Path
    reports_dir: Path
    seed_results: tuple[SwarmSeedResult, ...]
    seed_ledger: Path
    final_ranked_findings: Path
    final_summary: Path


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


def display_repo_path(cwd: Path, path: Path) -> str:
    repo_dir = cwd.resolve()
    candidate = path if path.is_absolute() else repo_dir / path
    try:
        return candidate.relative_to(repo_dir).as_posix()
    except ValueError:
        for entry in list_repo_file_entries(repo_dir):
            if entry.path == candidate:
                return entry.relative_path
        raise RuntimeError("Path is not tracked within the repo scope.")


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
        for entry in list_repo_file_entries(self.cwd):
            if self.scope_include and not _matches_any(entry.relative_path, self.scope_include):
                continue
            if _matches_any(entry.relative_path, self.scope_exclude):
                continue
            if path_glob and not PurePosixPath(entry.relative_path).match(path_glob):
                continue
            allowed.append(entry.path)
        return allowed

    def resolve_allowed_path(self, raw_path: str) -> Path:
        relative = _normalize_repo_relative_path(raw_path)
        allowed_by_relative = {
            self._display_path(path): path for path in self.allowed_paths()
        }
        if relative not in allowed_by_relative:
            raise RuntimeError("Path is outside the configured readable repo scope.")
        path = allowed_by_relative[relative]
        if not path.exists() or not path.is_file():
            raise RuntimeError("File does not exist.")
        return path

    def _display_path(self, path: Path) -> str:
        return display_repo_path(self.cwd, path)


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
    tracked_entries = list_repo_file_entries(cwd)
    inventory = "\n".join(f"- {entry.relative_path}" for entry in tracked_entries[:400])
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


def build_seed_input(
    *,
    seed_id: str,
    target_file: str,
    target_text: str,
    swarm_digest_text: str,
    shared_manifest_text: str,
) -> str:
    return "\n".join(
        [
            "Inspect the target file for one strongest black-hat seed finding.",
            "Return exactly one JSON object and no surrounding prose.",
            "Expected keys: outcome, severity_bucket, claim, evidence, related_files, notes.",
            "outcome must be finding or no_finding.",
            "severity_bucket must be high, medium, low, or none.",
            "evidence, related_files, and notes must be JSON arrays of concise strings.",
            f"Seed id: {seed_id}",
            f"Target file: {target_file}",
            "",
            "Shared manifest:",
            shared_manifest_text,
            "",
            "Swarm digest:",
            swarm_digest_text,
            "",
            f"Target file contents for {target_file}:",
            target_text,
        ]
    )


def normalize_seed_payload(
    *,
    payload: dict[str, Any],
    seed_id: str,
    target_file: str,
) -> SwarmSeedResult:
    outcome = str(payload.get("outcome", "") or "").strip().lower()
    if outcome not in {"finding", "no_finding"}:
        outcome = "finding" if str(payload.get("claim", "")).strip() else "no_finding"

    severity_bucket = str(payload.get("severity_bucket", "") or "").strip().lower()
    if severity_bucket not in {"high", "medium", "low", "none"}:
        severity_bucket = "none" if outcome == "no_finding" else "low"
    if outcome == "no_finding":
        severity_bucket = "none"

    claim = str(payload.get("claim", "") or "").strip()
    if outcome == "no_finding":
        claim = ""

    return SwarmSeedResult(
        seed_id=seed_id,
        target_file=target_file,
        outcome=outcome,
        severity_bucket=severity_bucket,
        claim=claim,
        evidence=tuple(_string_list(payload.get("evidence"))),
        related_files=tuple(_string_list(payload.get("related_files"))),
        notes=tuple(_string_list(payload.get("notes"))),
    )


def write_seed_ledger(path: Path, seed_results: list[SwarmSeedResult]) -> None:
    lines = ["# Seed ledger", ""]
    if not seed_results:
        lines.append("No eligible files were processed.")
    for result in seed_results:
        lines.extend(
            [
                f"## {result.seed_id}",
                f"- Target file: `{result.target_file}`",
                f"- Outcome: `{result.outcome}`",
                f"- Severity: `{result.severity_bucket}`",
                f"- Claim: {result.claim or '(none)'}",
                "",
            ]
        )
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def write_final_ranked_findings(path: Path, seed_results: list[SwarmSeedResult]) -> None:
    findings = sorted(
        [result for result in seed_results if result.outcome == "finding"],
        key=lambda item: (_severity_rank(item.severity_bucket), item.target_file),
    )
    lines = [
        "# Final ranked findings",
        "",
        "Thin sweep report only. Proof-stage promotion and duplicate grouping are not included yet.",
        "",
    ]
    if not findings:
        lines.append("No findings survived the sweep stage.")
    for index, result in enumerate(findings, start=1):
        lines.extend(
            [
                f"## {index}. {result.seed_id}",
                f"- Severity: `{result.severity_bucket}`",
                f"- Primary file: `{result.target_file}`",
                f"- Claim: {result.claim}",
            ]
        )
        if result.related_files:
            lines.append(
                "- Related files: " + ", ".join(f"`{item}`" for item in result.related_files)
            )
        if result.evidence:
            lines.append("- Evidence:")
            for item in result.evidence:
                lines.append(f"  - {item}")
        if result.notes:
            lines.append("- Notes:")
            for item in result.notes:
                lines.append(f"  - {item}")
        lines.append("")
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def write_final_summary(
    path: Path,
    seed_results: list[SwarmSeedResult],
    seed_ledger: Path,
    final_ranked_findings: Path,
) -> None:
    findings = [result for result in seed_results if result.outcome == "finding"]
    counts = {
        "high": sum(1 for result in findings if result.severity_bucket == "high"),
        "medium": sum(1 for result in findings if result.severity_bucket == "medium"),
        "low": sum(1 for result in findings if result.severity_bucket == "low"),
    }
    lines = [
        "# Final summary",
        "",
        f"- Eligible files processed: `{len(seed_results)}`",
        f"- Findings kept: `{len(findings)}`",
        f"- High findings: `{counts['high']}`",
        f"- Medium findings: `{counts['medium']}`",
        f"- Low findings: `{counts['low']}`",
        f"- Seed ledger: `{seed_ledger}`",
        f"- Ranked findings: `{final_ranked_findings}`",
    ]
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _write_seed_artifacts(seeds_dir: Path, seed_result: SwarmSeedResult) -> None:
    seed_prefix = seed_result.seed_id.lower().replace("-", "_")
    json_path = seeds_dir / f"{seed_prefix}.json"
    md_path = seeds_dir / f"{seed_prefix}.md"
    json_path.write_text(json.dumps(seed_result.to_dict(), indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_seed_markdown(seed_result), encoding="utf-8")


def render_seed_markdown(seed_result: SwarmSeedResult) -> str:
    lines = [
        f"# {seed_result.seed_id}",
        "",
        f"- Target file: `{seed_result.target_file}`",
        f"- Outcome: `{seed_result.outcome}`",
        f"- Severity: `{seed_result.severity_bucket}`",
    ]
    if seed_result.claim:
        lines.extend(["", "## Claim", seed_result.claim])
    if seed_result.evidence:
        lines.extend(["", "## Evidence"])
        for item in seed_result.evidence:
            lines.append(f"- {item}")
    if seed_result.related_files:
        lines.extend(["", "## Related files"])
        for item in seed_result.related_files:
            lines.append(f"- `{item}`")
    if seed_result.notes:
        lines.extend(["", "## Notes"])
        for item in seed_result.notes:
            lines.append(f"- {item}")
    return "\n".join(lines).strip() + "\n"


def list_repo_file_entries(cwd: Path) -> list[RepoFileEntry]:
    repo_dir = cwd.resolve()
    git_entries = _git_tracked_files(repo_dir)
    if git_entries is not None:
        return git_entries

    files: list[RepoFileEntry] = []
    for path in sorted(repo_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(repo_dir).as_posix()
        if _is_runtime_managed_relative(relative):
            continue
        files.append(RepoFileEntry(relative_path=relative, path=path))
    return files


def list_repo_files(cwd: Path) -> list[Path]:
    return [entry.path for entry in list_repo_file_entries(cwd)]


def list_eligible_swarm_files(cwd: Path, loaded) -> list[Path]:
    repo_dir = cwd.resolve()
    scope_include = loaded.effective.scope.include
    scope_exclude = loaded.effective.scope.exclude
    swarm_config = loaded.effective.swarm
    if swarm_config is None:
        raise RuntimeError("Swarm config is not available.")

    eligible: list[Path] = []
    for entry in list_repo_file_entries(repo_dir):
        if scope_include and not _matches_any(entry.relative_path, scope_include):
            continue
        if _matches_any(entry.relative_path, scope_exclude):
            continue
        if _matches_swarm_profile(entry.relative_path, swarm_config.eligible_file_profile):
            eligible.append(entry.path)
    return eligible


def run_swarm_sweep(
    *,
    cwd: Path,
    loaded,
    provider: OpenAIResponsesProvider,
    run_dir: Path,
    swarm_digest_path: Path,
    shared_manifest_path: Path,
    eligible_files: list[Path],
) -> SwarmSweepResult:
    if loaded.effective.swarm is None:
        raise RuntimeError("Swarm config is not available.")

    swarm_root = run_dir / "swarm"
    seeds_dir = swarm_root / "seeds"
    proofs_dir = swarm_root / "proofs"
    reports_dir = swarm_root / "reports"
    seeds_dir.mkdir(parents=True, exist_ok=True)
    proofs_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    swarm_digest_text = swarm_digest_path.read_text(encoding="utf-8")
    shared_manifest_text = shared_manifest_path.read_text(encoding="utf-8")
    tools = RepoReadOnlyTools(
        cwd=cwd,
        scope_include=loaded.effective.scope.include,
        scope_exclude=loaded.effective.scope.exclude,
    )

    seed_results: list[SwarmSeedResult] = []
    for index, target_path in enumerate(eligible_files, start=1):
        seed_id = f"SEED-{index:03d}"
        target_file = display_repo_path(cwd, target_path)
        target_text = target_path.read_text(encoding="utf-8", errors="replace")
        result = provider.start_foreground_turn(
            model=loaded.effective.swarm.sweep_model,
            reasoning_effort="low",
            instructions=loaded.effective.swarm.prompt_file.read_text(encoding="utf-8"),
            input_text=build_seed_input(
                seed_id=seed_id,
                target_file=target_file,
                target_text=target_text,
                swarm_digest_text=swarm_digest_text,
                shared_manifest_text=shared_manifest_text,
            ),
            previous_response_id=None,
            tools=tools.schemas(),
            tool_executor=tools.run,
        )
        payload = _parse_json_object(result.final_text)
        seed_result = normalize_seed_payload(
            payload=payload,
            seed_id=seed_id,
            target_file=target_file,
        )
        _write_seed_artifacts(seeds_dir, seed_result)
        seed_results.append(seed_result)

    seed_ledger = reports_dir / "seed_ledger.md"
    final_ranked_findings = reports_dir / "final_ranked_findings.md"
    final_summary = reports_dir / "final_summary.md"
    write_seed_ledger(seed_ledger, seed_results)
    write_final_ranked_findings(final_ranked_findings, seed_results)
    write_final_summary(final_summary, seed_results, seed_ledger, final_ranked_findings)

    return SwarmSweepResult(
        seeds_dir=seeds_dir,
        proofs_dir=proofs_dir,
        reports_dir=reports_dir,
        seed_results=tuple(seed_results),
        seed_ledger=seed_ledger,
        final_ranked_findings=final_ranked_findings,
        final_summary=final_summary,
    )


def _git_tracked_files(repo_dir: Path) -> list[RepoFileEntry] | None:
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

    files: list[RepoFileEntry] = []
    for line in result.stdout.splitlines():
        value = line.strip()
        if not value:
            continue
        path = repo_dir / value
        if path.is_file():
            files.append(RepoFileEntry(relative_path=value, path=path))
    return sorted(files, key=lambda entry: entry.relative_path)


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


def _severity_rank(severity_bucket: str) -> int:
    order = {"high": 0, "medium": 1, "low": 2, "none": 3}
    return order.get(severity_bucket, 4)


def _matches_swarm_profile(relative_path: str, profile: str) -> bool:
    if profile == "all_tracked":
        return True

    path = PurePosixPath(relative_path)
    name = path.name.lower()
    extension = path.suffix.lower()
    parts = tuple(part.lower() for part in path.parts)

    if extension in CODE_EXTENSIONS or extension in CONFIG_EXTENSIONS:
        return True
    if name in CONFIG_FILENAMES:
        return True
    if "tests" in parts or name.startswith("test_") or name.endswith("_test.py"):
        return True
    return False


def _matches_any(relative_path: str, globs: tuple[str, ...]) -> bool:
    posix = PurePosixPath(relative_path)
    return any(posix.match(pattern) for pattern in globs)


def _normalize_repo_relative_path(raw_path: str) -> str:
    path = PurePosixPath(str(raw_path).replace("\\", "/"))
    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise RuntimeError("Path escapes the repo root.")
            parts.pop()
            continue
        parts.append(part)
    normalized = "/".join(parts)
    if not normalized:
        raise RuntimeError("Path is empty.")
    return normalized


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

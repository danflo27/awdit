from __future__ import annotations

import hashlib
import json
import math
import random
import re
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from paths import managed_runtime_root_names
from provider_openai import (
    OpenAIResponsesProvider,
    ProviderBackgroundHandle,
    ProviderToolCall,
    ProviderTurnResult,
    ToolTraceRecord,
)
from repo_memory import (
    RepoIdentity,
    danger_map_paths,
    migrate_legacy_repo_memory_dir,
    resolve_repo_identity,
)
from state_db import load_learned_model_limit, save_learned_model_limit

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
DEFAULT_SWARM_MAX_PARALLEL = 8
DEFAULT_SWARM_MAX_RETRIES = 1
DEFAULT_SWARM_RATE_LIMIT_COOLDOWN_SECONDS = 5.0
DEFAULT_SWARM_RATE_LIMIT_JITTER_SECONDS = 0.15
DEFAULT_SWARM_POLL_INTERVAL_SECONDS = 0.05
DEFAULT_SWARM_ESTIMATED_CHARS_PER_TOKEN = 4
DEFAULT_SWARM_TPM_HEADROOM_FRACTION = 0.85
DEFAULT_SWARM_TPM_WINDOW_SECONDS = 60.0
SWARM_STAGE_PRESET_BEHAVIORS: dict[str, dict[str, Any]] = {
    "safe": {
        "hard_safe": True,
        "bootstrap_parallel_limit": 1,
        "rate_limit_strike_limit": 1,
    },
    "balanced": {
        "hard_safe": True,
        "bootstrap_parallel_limit": 1,
        "rate_limit_strike_limit": 2,
    },
    "fast": {
        "hard_safe": False,
        "bootstrap_parallel_limit": None,
        "rate_limit_strike_limit": 0,
    },
}
PROOF_STATE_VALUES = (
    "hypothesized",
    "path_grounded",
    "written_proof",
    "executed_proof",
)
REPORTABLE_PROOF_STATES = {"written_proof", "executed_proof"}
PROOF_CONTRADICTION_PHRASES = (
    "not reportable",
    "does not meet the report bar",
    "insufficient proof",
    "theoretical only",
    "hardening concern",
)
RATE_LIMIT_RETRY_PATTERN = re.compile(
    r"please try again in\s+([0-9]+(?:\.[0-9]+)?)\s*(ms|milliseconds?|s|sec(?:onds?)?)\b",
    re.IGNORECASE,
)
RATE_LIMIT_TPM_LIMIT_PATTERN = re.compile(
    r"tokens per min\s*\(tpm\):\s*limit\s*([0-9]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DangerMapResult:
    identity: RepoIdentity
    danger_map_md: Path
    danger_map_json: Path
    repo_comments_md: Path
    payload: dict[str, Any]


@dataclass(frozen=True)
class SwarmPromptAsset:
    stage: str
    source_path: Path
    snapshot_path: Path
    sha256: str
    prompt_cache_key: str

    def read_text(self) -> str:
        return self.snapshot_path.read_text(encoding="utf-8")

    def to_dict(self) -> dict[str, str]:
        return {
            "stage": self.stage,
            "source_path": str(self.source_path),
            "snapshot_path": str(self.snapshot_path),
            "sha256": self.sha256,
            "prompt_cache_key": self.prompt_cache_key,
        }


@dataclass(frozen=True)
class SwarmPromptBundle:
    prompts_dir: Path
    manifest_path: Path
    danger_map: SwarmPromptAsset
    seed: SwarmPromptAsset
    proof: SwarmPromptAsset

    def stage(self, stage_name: str) -> SwarmPromptAsset:
        if stage_name == "danger_map":
            return self.danger_map
        if stage_name == "seed":
            return self.seed
        if stage_name == "proof":
            return self.proof
        raise KeyError(f"Unknown swarm prompt stage: {stage_name}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_path": str(self.manifest_path),
            "stages": {
                "danger_map": self.danger_map.to_dict(),
                "seed": self.seed.to_dict(),
                "proof": self.proof.to_dict(),
            },
        }


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
class SwarmIssueCandidate:
    case_id: str
    primary_seed_id: str
    primary_target_file: str
    severity_bucket: str
    claim: str
    evidence: tuple[str, ...]
    related_files: tuple[str, ...]
    notes: tuple[str, ...]
    seed_ids: tuple[str, ...]
    duplicate_seed_ids: tuple[str, ...]
    target_files: tuple[str, ...]
    grouping_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "primary_seed_id": self.primary_seed_id,
            "primary_target_file": self.primary_target_file,
            "severity_bucket": self.severity_bucket,
            "claim": self.claim,
            "evidence": list(self.evidence),
            "related_files": list(self.related_files),
            "notes": list(self.notes),
            "seed_ids": list(self.seed_ids),
            "duplicate_seed_ids": list(self.duplicate_seed_ids),
            "target_files": list(self.target_files),
            "grouping_keys": list(self.grouping_keys),
        }


@dataclass(frozen=True)
class SwarmProofResult:
    case_id: str
    primary_seed_id: str
    primary_target_file: str
    severity_bucket: str
    seed_ids: tuple[str, ...]
    duplicate_seed_ids: tuple[str, ...]
    outcome: str
    proof_state: str
    claim: str
    summary: str
    preconditions: tuple[str, ...]
    repro_steps: tuple[str, ...]
    citations: tuple[str, ...]
    notes: tuple[str, ...]
    filter_reason: str

    @property
    def meets_report_bar(self) -> bool:
        return (
            self.outcome == "reportable"
            and self.proof_state in REPORTABLE_PROOF_STATES
            and _reportable_contradiction(summary=self.summary, notes=self.notes) is None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "primary_seed_id": self.primary_seed_id,
            "primary_target_file": self.primary_target_file,
            "severity_bucket": self.severity_bucket,
            "seed_ids": list(self.seed_ids),
            "duplicate_seed_ids": list(self.duplicate_seed_ids),
            "outcome": self.outcome,
            "proof_state": self.proof_state,
            "claim": self.claim,
            "summary": self.summary,
            "preconditions": list(self.preconditions),
            "repro_steps": list(self.repro_steps),
            "citations": list(self.citations),
            "notes": list(self.notes),
            "filter_reason": self.filter_reason,
        }


@dataclass(frozen=True)
class SwarmSweepResult:
    seeds_dir: Path
    proofs_dir: Path
    reports_dir: Path
    tool_trace_log: Path
    seed_results: tuple[SwarmSeedResult, ...]
    issue_candidates: tuple[SwarmIssueCandidate, ...]
    proof_results: tuple[SwarmProofResult, ...]
    seed_ledger: Path
    case_groups: Path
    final_ranked_findings: Path
    final_summary: Path
    usage_summary: Path


@dataclass(frozen=True)
class SwarmWorkerJob:
    worker_id: str
    worker_type: str
    lease_key: str
    model: str
    reasoning_effort: str | None
    instructions: str
    input_text: str
    prompt_cache_key: str | None
    text_format: dict[str, Any] | None
    tools: tuple[dict[str, Any], ...]
    progress_label: str | None = None
    progress_action: str | None = None
    input_variants: tuple[str, ...] = ()
    input_variant_index: int = 0


@dataclass
class ActiveSwarmWorker:
    job: SwarmWorkerJob
    handle: ProviderBackgroundHandle | None
    tool_traces: list[ToolTraceRecord]
    started_at: float
    pending_continuation_response_id: str | None = None
    pending_continuation_input: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class SwarmSeedRequestVolume:
    job_count: int
    total_estimated_tokens: int
    peak_parallel_estimated_tokens: int
    max_job_estimated_tokens: int
    max_job_target_file: str


@dataclass(frozen=True)
class SwarmWorkerFailureDiagnostic:
    stage: str
    worker_id: str
    lease_key: str
    failure_message: str
    response_id: str | None = None
    raw_final_text: str = ""

    def render_summary(self) -> str:
        return f"{self.worker_id} ({self.lease_key}): {self.failure_message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "worker_id": self.worker_id,
            "lease_key": self.lease_key,
            "failure_message": self.failure_message,
            "response_id": self.response_id,
            "raw_final_text": self.raw_final_text,
        }


class SwarmWorkerFailure(RuntimeError):
    def __init__(self, diagnostics: list[SwarmWorkerFailureDiagnostic]) -> None:
        self.diagnostics = tuple(diagnostics)
        super().__init__(
            "Swarm worker failure: "
            + "; ".join(item.render_summary() for item in self.diagnostics)
        )

    @property
    def primary_diagnostic(self) -> SwarmWorkerFailureDiagnostic | None:
        if not self.diagnostics:
            return None
        return self.diagnostics[0]


class SwarmStageAbort(SwarmWorkerFailure):
    def __init__(
        self,
        *,
        stage_name: str,
        diagnostics: list[SwarmWorkerFailureDiagnostic],
        skipped_worker_ids: tuple[str, ...],
    ) -> None:
        self.stage_name = stage_name
        self.skipped_worker_ids = skipped_worker_ids
        super().__init__(diagnostics)


SwarmProgressCallback = Callable[[str, dict[str, Any]], None]


DANGER_MAP_RESPONSE_FORMAT = {
    "type": "json_schema",
    "name": "danger_map_response",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "trust_boundaries": {"type": "array", "items": {"type": "string"}},
            "risky_sinks": {"type": "array", "items": {"type": "string"}},
            "auth_assumptions": {"type": "array", "items": {"type": "string"}},
            "hot_paths": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "trust_boundaries",
            "risky_sinks",
            "auth_assumptions",
            "hot_paths",
            "notes",
        ],
        "additionalProperties": False,
    },
}

SEED_RESPONSE_FORMAT = {
    "type": "json_schema",
    "name": "swarm_seed_response",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "enum": ["finding", "no_finding"]},
            "severity_bucket": {"type": "string", "enum": ["high", "medium", "low", "none"]},
            "claim": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "related_files": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["outcome", "severity_bucket", "claim", "evidence", "related_files", "notes"],
        "additionalProperties": False,
    },
}

PROOF_RESPONSE_FORMAT = {
    "type": "json_schema",
    "name": "swarm_proof_response",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "enum": ["reportable", "not_reportable"]},
            "proof_state": {"type": "string", "enum": list(PROOF_STATE_VALUES)},
            "claim": {"type": "string"},
            "summary": {"type": "string"},
            "preconditions": {"type": "array", "items": {"type": "string"}},
            "repro_steps": {"type": "array", "items": {"type": "string"}},
            "citations": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "array", "items": {"type": "string"}},
            "filter_reason": {"type": "string"},
        },
        "required": [
            "outcome",
            "proof_state",
            "claim",
            "summary",
            "preconditions",
            "repro_steps",
            "citations",
            "notes",
            "filter_reason",
        ],
        "additionalProperties": False,
    },
}


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


def freeze_swarm_prompt_bundle(*, run_dir: Path, loaded) -> SwarmPromptBundle:
    swarm_config = loaded.effective.swarm
    if swarm_config is None:
        raise RuntimeError("Swarm config is not available.")

    prompts_dir = run_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    stage_sources = {
        "danger_map": swarm_config.prompts.danger_map,
        "seed": swarm_config.prompts.seed,
        "proof": swarm_config.prompts.proof,
    }
    assets: dict[str, SwarmPromptAsset] = {}
    manifest_payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stages": {},
    }
    for stage, source_path in stage_sources.items():
        text = source_path.read_text(encoding="utf-8")
        sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        snapshot_path = prompts_dir / f"swarm_{stage}.md"
        snapshot_path.write_text(text, encoding="utf-8")
        prompt_cache_key = f"awdit:swarm:{stage}:{sha256[:16]}"
        asset = SwarmPromptAsset(
            stage=stage,
            source_path=source_path,
            snapshot_path=snapshot_path,
            sha256=sha256,
            prompt_cache_key=prompt_cache_key,
        )
        assets[stage] = asset
        manifest_payload["stages"][stage] = asset.to_dict()

    manifest_path = prompts_dir / "swarm_prompt_bundle.json"
    manifest_path.write_text(json.dumps(manifest_payload, indent=2) + "\n", encoding="utf-8")
    return SwarmPromptBundle(
        prompts_dir=prompts_dir,
        manifest_path=manifest_path,
        danger_map=assets["danger_map"],
        seed=assets["seed"],
        proof=assets["proof"],
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
    def __init__(
        self,
        *,
        cwd: Path,
        scope_include: tuple[str, ...],
        scope_exclude: tuple[str, ...],
        extra_allowed_paths: tuple[Path, ...] = (),
    ) -> None:
        self.cwd = cwd.resolve()
        self.scope_include = scope_include
        self.scope_exclude = scope_exclude
        self.extra_allowed_paths = tuple(path.resolve() for path in extra_allowed_paths)

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": "list_scope_files",
                "description": "List allowed repo files and run-staged shared resources that are in scope for swarm inspection.",
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
                "description": "Read an allowed repo file or current-run staged shared resource inside the current swarm scope. Supports paged reads by line number.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "max_lines": {"type": "integer", "minimum": 1, "maximum": 500},
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
            start_line = int(arguments.get("start_line", 1) or 1)
            if start_line <= 0:
                raise RuntimeError("read_file start_line must be >= 1.")
            max_lines = int(arguments.get("max_lines", 200) or 200)
            if max_lines <= 0:
                raise RuntimeError("read_file max_lines must be >= 1.")
            max_chars = int(arguments.get("max_chars", 12000) or 12000)
            path = self.resolve_allowed_path(raw_path)
            text = path.read_text(encoding="utf-8", errors="replace")
            all_lines = text.splitlines()
            if text.endswith("\n"):
                all_lines.append("")
            raw_line_count = len(all_lines) or 1
            if start_line > raw_line_count:
                raise RuntimeError("read_file start_line is beyond the end of the file.")
            end_line_exclusive = min(raw_line_count + 1, start_line + max_lines)
            selected_lines = all_lines[start_line - 1 : end_line_exclusive - 1]
            selected_text = "\n".join(selected_lines)
            visible_text = selected_text[:max_chars]
            numbered_text = _with_line_numbers(visible_text, start_line=start_line)
            visible_line_count = max(1, len(visible_text.splitlines()) or (1 if visible_text else 0))
            end_line = min(raw_line_count, start_line + visible_line_count - 1)
            truncated_after = end_line < raw_line_count or len(selected_text) > max_chars
            return json.dumps(
                {
                    "path": self._display_path(path),
                    "start_line": start_line,
                    "end_line": end_line,
                    "content": numbered_text,
                    "truncated": len(text) > max_chars or truncated_after,
                    "truncated_before": start_line > 1,
                    "truncated_after": truncated_after,
                    "raw_line_count": raw_line_count,
                    "raw_char_count": len(text),
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
        for path in self.extra_allowed_paths:
            if not path.exists() or not path.is_file():
                continue
            display_path = self._display_path(path)
            if path_glob and not PurePosixPath(display_path).match(path_glob):
                continue
            allowed.append(path)
        unique_allowed: list[Path] = []
        seen: set[str] = set()
        for path in allowed:
            display_path = self._display_path(path)
            if display_path in seen:
                continue
            seen.add(display_path)
            unique_allowed.append(path)
        return unique_allowed

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
    prompt_bundle: SwarmPromptBundle,
    guidance_notes: tuple[str, ...] = (),
) -> DangerMapResult:
    swarm_config = loaded.effective.swarm
    if swarm_config is None:
        raise RuntimeError("Swarm config is not available.")

    identity = resolve_repo_identity(cwd)
    migrate_legacy_repo_memory_dir(cwd, identity)
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
    prompt_asset = prompt_bundle.danger_map
    result = _run_swarm_background_worker(
        provider=provider,
        job=SwarmWorkerJob(
            worker_id="danger_map",
            worker_type="danger_map",
            lease_key=f"repo:{identity.repo_key}",
            model=swarm_config.sweep_model,
            reasoning_effort=swarm_config.reasoning.danger_map,
            instructions=prompt_asset.read_text(),
            input_text=input_text,
            prompt_cache_key=prompt_asset.prompt_cache_key,
            text_format=DANGER_MAP_RESPONSE_FORMAT,
            tools=tuple(tools.schemas()),
        ),
        tool_executor=tools.run,
        rate_limit_max_retries=swarm_config.rate_limit_max_retries,
        stage_name="danger_map",
        cwd=cwd,
        provider_name=loaded.effective.active_provider,
    )
    payload = _parse_json_object(result.final_text)
    normalized = parse_danger_map_payload(
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
    payload = {
        "task_type": "danger_map",
        "repo": {
            "repo_name": identity.repo_name,
            "repo_key": identity.repo_key,
            "identity_source": identity.source_kind,
        },
        "scope": {
            "include": list(loaded.effective.scope.include),
            "exclude": list(loaded.effective.scope.exclude),
        },
        "user_guidance": list(guidance_notes),
        "tracked_inventory": [entry.relative_path for entry in tracked_entries[:400]],
    }
    return json.dumps(payload, indent=2)


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


def parse_danger_map_payload(
    *,
    payload: dict[str, Any],
    identity: RepoIdentity,
    guidance_notes: tuple[str, ...],
) -> dict[str, Any]:
    _require_payload_keys(
        payload,
        ("trust_boundaries", "risky_sinks", "auth_assumptions", "hot_paths", "notes"),
    )
    return normalize_danger_map_payload(
        payload=payload,
        identity=identity,
        guidance_notes=guidance_notes,
    )


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
    swarm_digest_text: str,
    shared_manifest_text: str,
    target_size_bytes: int,
    context_level: str = "compact",
) -> str:
    swarm_digest_summary = _compact_seed_context(
        swarm_digest_text,
        max_chars=2400 if context_level == "compact" else 800,
    )
    shared_manifest_summary = _compact_seed_context(
        shared_manifest_text,
        max_chars=1400 if context_level == "compact" else 500,
    )
    payload = {
        "task_type": "seed_file",
        "seed_id": seed_id,
        "lease_key": f"file:{target_file}",
        "target_file": target_file,
        "context_level": context_level,
        "target_file_metadata": {
            "path": target_file,
            "size_bytes": target_size_bytes,
        },
        "first_read_request": {
            "path": target_file,
            "start_line": 1,
            "max_lines": 200,
            "max_chars": 12000,
        },
        "operator_notes": [
            "Inspect the target file with read_file first.",
            "Use paged read_file calls for large files instead of trying to load everything at once.",
        ],
        "shared_manifest_summary": shared_manifest_summary,
        "swarm_digest_summary": swarm_digest_summary,
    }
    return json.dumps(payload, indent=2)


def _staged_shared_resource_files(shared_manifest_path: Path) -> tuple[Path, ...]:
    staged_root = shared_manifest_path.parent / "staged"
    if not staged_root.exists():
        return ()

    staged_files: list[Path] = []
    for path in sorted(staged_root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        staged_files.append(path.resolve())
    return tuple(staged_files)


def _compact_seed_context(text: str, *, max_chars: int) -> str:
    compact_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if line.startswith("#"):
            compact_lines.append(line)
            continue
        if line.startswith("- "):
            compact_lines.append(line)
            continue
        if len(compact_lines) < 12:
            compact_lines.append(line)
    compact_text = "\n".join(compact_lines).strip()
    if len(compact_text) <= max_chars:
        return compact_text
    return compact_text[:max_chars].rstrip() + "\n..."


def render_seed_instructions(
    *,
    template: str,
    target_file: str,
    output_path: str,
) -> str:
    return (
        template.replace("{{target_file}}", target_file).replace("{{output_path}}", output_path)
    )


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + DEFAULT_SWARM_ESTIMATED_CHARS_PER_TOKEN - 1) // DEFAULT_SWARM_ESTIMATED_CHARS_PER_TOKEN)


def _estimate_job_request_tokens(job: SwarmWorkerJob) -> int:
    return _estimate_text_tokens(job.instructions) + _estimate_text_tokens(job.input_text)


def _coerce_nonnegative_int(value: Any) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, normalized)


def _new_usage_totals() -> dict[str, Any]:
    return {
        "responses": 0,
        "tool_calls_requested": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "billable_input_tokens_estimate": 0,
        "billable_tokens_estimate": 0,
        "peak_input_tokens": 0,
        "peak_total_tokens": 0,
        "response_ids": [],
        "models": {},
        "_response_usage": {},
    }


def _refresh_usage_totals(totals: dict[str, Any]) -> None:
    response_usage = totals.get("_response_usage", {})
    totals["responses"] = len(response_usage)
    totals["input_tokens"] = 0
    totals["output_tokens"] = 0
    totals["total_tokens"] = 0
    totals["cached_input_tokens"] = 0
    totals["reasoning_output_tokens"] = 0
    totals["peak_input_tokens"] = 0
    totals["peak_total_tokens"] = 0
    totals["response_ids"] = list(response_usage.keys())
    totals["models"] = {}

    for response_id in totals["response_ids"]:
        sample = response_usage[response_id]
        input_tokens = _coerce_nonnegative_int(sample.get("input_tokens"))
        output_tokens = _coerce_nonnegative_int(sample.get("output_tokens"))
        total_tokens = _coerce_nonnegative_int(sample.get("total_tokens"))
        cached_input_tokens = _coerce_nonnegative_int(sample.get("cached_input_tokens"))
        reasoning_output_tokens = _coerce_nonnegative_int(sample.get("reasoning_output_tokens"))

        totals["input_tokens"] += input_tokens
        totals["output_tokens"] += output_tokens
        totals["total_tokens"] += total_tokens
        totals["cached_input_tokens"] += cached_input_tokens
        totals["reasoning_output_tokens"] += reasoning_output_tokens
        totals["peak_input_tokens"] = max(totals["peak_input_tokens"], input_tokens)
        totals["peak_total_tokens"] = max(totals["peak_total_tokens"], total_tokens)

        model_name = str(sample.get("model", "") or "").strip() or "unknown"
        model_totals = totals["models"].setdefault(model_name, {"responses": 0, "total_tokens": 0})
        model_totals["responses"] += 1
        model_totals["total_tokens"] += total_tokens

    totals["billable_input_tokens_estimate"] = max(
        0,
        totals["input_tokens"] - totals["cached_input_tokens"],
    )
    totals["billable_tokens_estimate"] = max(
        0,
        totals["billable_input_tokens_estimate"] + totals["output_tokens"],
    )


def _accumulate_provider_usage(totals: dict[str, Any], data: dict[str, Any]) -> None:
    response_id = str(data.get("response_id", "") or "").strip()
    if not response_id:
        response_id = f"anonymous_{len(totals['_response_usage']) + 1}"
    totals["_response_usage"][response_id] = {
        "model": str(data.get("model", "") or "").strip(),
        "input_tokens": _coerce_nonnegative_int(data.get("input_tokens")),
        "output_tokens": _coerce_nonnegative_int(data.get("output_tokens")),
        "total_tokens": _coerce_nonnegative_int(data.get("total_tokens")),
        "cached_input_tokens": _coerce_nonnegative_int(data.get("cached_input_tokens")),
        "reasoning_output_tokens": _coerce_nonnegative_int(data.get("reasoning_output_tokens")),
    }
    _refresh_usage_totals(totals)


def _public_usage_totals(totals: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in totals.items()
        if key != "_response_usage"
    }


def _swarm_job_label(job: SwarmWorkerJob) -> str:
    label = str(job.progress_label or "").strip()
    if label:
        return label
    _, _, lease_value = job.lease_key.partition(":")
    if lease_value.strip():
        return lease_value.strip()
    return job.worker_id


def _swarm_job_action(job: SwarmWorkerJob) -> str:
    action = str(job.progress_action or "").strip()
    if action:
        return action
    label = _swarm_job_label(job)
    if job.worker_type == "seed_file":
        return f"inspect {label}"
    if job.worker_type == "proof_issue":
        return f"validate promoted finding for {label}"
    if job.worker_type == "danger_map":
        return f"map repo attack surface for {label}"
    return f"process {label}"


def _swarm_progress_payload(stage_name: str, job: SwarmWorkerJob) -> dict[str, Any]:
    return {
        "stage_name": stage_name,
        "worker_id": job.worker_id,
        "worker_type": job.worker_type,
        "lease_key": job.lease_key,
        "label": _swarm_job_label(job),
        "action": _swarm_job_action(job),
        "model": job.model,
        "reasoning_effort": job.reasoning_effort,
    }


def _tool_call_summary(*, job: SwarmWorkerJob, tool_name: str, arguments: dict[str, Any]) -> str:
    label = _swarm_job_label(job)
    if tool_name == "read_file":
        path = str(arguments.get("path", label) or label)
        start_line = max(1, _coerce_nonnegative_int(arguments.get("start_line")) or 1)
        max_lines = max(1, _coerce_nonnegative_int(arguments.get("max_lines")) or 200)
        end_line = start_line + max_lines - 1
        if path == label:
            return f"reading {path} lines {start_line}-{end_line} for initial context"
        return f"reading {path} lines {start_line}-{end_line} while inspecting {label}"
    if tool_name == "list_scope_files":
        path_glob = str(arguments.get("path_glob", "") or "").strip()
        if path_glob:
            return f"scanning {path_glob} for nearby clues while inspecting {label}"
        return f"scanning the readable scope for nearby clues while inspecting {label}"
    return f"using {tool_name} while inspecting {label}"


def _append_tool_trace_records(
    *,
    path: Path | None,
    stage_name: str,
    job: SwarmWorkerJob,
    traces: tuple[ToolTraceRecord, ...],
) -> None:
    if path is None or not traces:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for trace in traces:
            payload = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "stage": stage_name,
                "worker_id": job.worker_id,
                "response_id": trace.response_id,
                "tool": trace.name,
                "arguments": trace.arguments,
                "summary": _tool_call_summary(job=job, tool_name=trace.name, arguments=trace.arguments),
                "output_meta": trace.output_meta,
            }
            handle.write(json.dumps(payload) + "\n")


def _degrade_worker_job(job: SwarmWorkerJob) -> SwarmWorkerJob | None:
    next_index = job.input_variant_index + 1
    if next_index >= len(job.input_variants):
        return None
    return replace(
        job,
        input_text=job.input_variants[next_index],
        input_variant_index=next_index,
    )


def _emit_swarm_progress(
    progress_callback: SwarmProgressCallback | None,
    event_type: str,
    **data: Any,
) -> None:
    if progress_callback is None:
        return
    progress_callback(event_type, data)


@dataclass
class SwarmRunMetrics:
    path: Path
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    status: str = "in_progress"
    finished_at: str | None = None
    failure_stage: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        self._started_monotonic = time.monotonic()
        self._finished_monotonic: float | None = None
        self._totals = _new_usage_totals()
        self._stages: dict[str, dict[str, Any]] = {}
        self._workers: dict[str, dict[str, Any]] = {}
        self._stage_worker_keys: dict[str, set[str]] = {}
        self._limiter_snapshot = {
            "preset": None,
            "hard_safe": True,
            "model_limit_tpm": None,
            "headroom_fraction": DEFAULT_SWARM_TPM_HEADROOM_FRACTION,
            "current_stage_parallel_ceiling": 1,
            "stage_degraded_after_rate_limit": False,
            "rate_limit_strikes": 0,
            "limiter_wait_seconds": 0.0,
            "blocked_launches": 0,
            "blocked_continuations": 0,
            "degrade_events": 0,
            "stage_name": None,
        }

    def register_stage_jobs(self, *, stage_name: str, jobs: list[SwarmWorkerJob]) -> None:
        for job in jobs:
            self._ensure_worker(stage_name=stage_name, job=job)
        self.write()

    def record_attempt_started(
        self,
        *,
        stage_name: str,
        job: SwarmWorkerJob,
        started_at_monotonic: float,
    ) -> None:
        stage = self._ensure_stage(stage_name)
        worker = self._ensure_worker(stage_name=stage_name, job=job)
        started_at = datetime.now().isoformat(timespec="seconds")
        if stage["first_started_at"] is None:
            stage["first_started_at"] = started_at
        if worker["first_started_at"] is None:
            worker["first_started_at"] = started_at
        worker["status"] = "running"
        worker["attempts"] += 1
        worker["_active_attempt_started_at"] = started_at_monotonic
        self.write()

    def record_provider_event(
        self,
        *,
        stage_name: str,
        job: SwarmWorkerJob,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        stage = self._ensure_stage(stage_name)
        worker = self._ensure_worker(stage_name=stage_name, job=job)
        if event_type == "provider_usage":
            _accumulate_provider_usage(self._totals, data)
            _accumulate_provider_usage(stage["totals"], data)
            _accumulate_provider_usage(worker["totals"], data)
        elif event_type == "tool_calls_requested":
            count = _coerce_nonnegative_int(data.get("count"))
            self._totals["tool_calls_requested"] += count
            stage["totals"]["tool_calls_requested"] += count
            worker["totals"]["tool_calls_requested"] += count
        else:
            return
        self.write()

    def record_retry(
        self,
        *,
        stage_name: str,
        job: SwarmWorkerJob,
        reason: str,
        delay_seconds: float | None = None,
    ) -> None:
        stage = self._ensure_stage(stage_name)
        worker = self._ensure_worker(stage_name=stage_name, job=job)
        stage["retry_events"] += 1
        worker["retry_events"] += 1
        worker["status"] = "pending_retry"
        worker["last_retry_reason"] = reason
        if delay_seconds is not None:
            worker["last_retry_delay_seconds"] = round(max(0.0, delay_seconds), 6)
        if reason == "rate_limit":
            stage["rate_limit_retry_events"] += 1
            worker["rate_limit_retry_events"] += 1
        self.write()

    def record_wait(
        self,
        *,
        stage_name: str,
        continuation: bool,
        seconds: float,
    ) -> None:
        stage = self._ensure_stage(stage_name)
        normalized = round(max(0.0, seconds), 6)
        stage["limiter_wait_seconds"] = round(stage["limiter_wait_seconds"] + normalized, 6)
        self._limiter_snapshot["limiter_wait_seconds"] = round(
            float(self._limiter_snapshot.get("limiter_wait_seconds", 0.0)) + normalized,
            6,
        )
        key = "blocked_continuations" if continuation else "blocked_launches"
        stage[key] += 1
        self._limiter_snapshot[key] = int(self._limiter_snapshot.get(key, 0)) + 1
        self.write()

    def record_degrade(self, *, stage_name: str, job: SwarmWorkerJob) -> None:
        stage = self._ensure_stage(stage_name)
        stage["degrade_events"] += 1
        self._limiter_snapshot["degrade_events"] = int(
            self._limiter_snapshot.get("degrade_events", 0)
        ) + 1
        worker = self._ensure_worker(stage_name=stage_name, job=job)
        worker["degrade_events"] += 1
        self.write()

    def update_limiter_state(self, *, stage_name: str, limiter_state: dict[str, Any]) -> None:
        stage = self._ensure_stage(stage_name)
        stage["limiter"] = {
            "preset": limiter_state.get("preset"),
            "hard_safe": limiter_state.get("hard_safe"),
            "model_limit_tpm": limiter_state.get("model_limit_tpm"),
            "headroom_fraction": limiter_state.get("headroom_fraction"),
            "current_stage_parallel_ceiling": limiter_state.get("current_stage_parallel_ceiling"),
            "stage_degraded_after_rate_limit": limiter_state.get("stage_degraded_after_rate_limit"),
            "rate_limit_strikes": limiter_state.get("rate_limit_strikes"),
            "limiter_wait_seconds": stage.get("limiter_wait_seconds", 0.0),
            "blocked_launches": stage.get("blocked_launches", 0),
            "blocked_continuations": stage.get("blocked_continuations", 0),
            "degrade_events": stage.get("degrade_events", 0),
        }
        self._limiter_snapshot = {
            "preset": limiter_state.get("preset"),
            "hard_safe": limiter_state.get("hard_safe", True),
            "model_limit_tpm": limiter_state.get("model_limit_tpm"),
            "headroom_fraction": limiter_state.get(
                "headroom_fraction", DEFAULT_SWARM_TPM_HEADROOM_FRACTION
            ),
            "current_stage_parallel_ceiling": limiter_state.get("current_stage_parallel_ceiling", 1),
            "stage_degraded_after_rate_limit": limiter_state.get(
                "stage_degraded_after_rate_limit", False
            ),
            "rate_limit_strikes": limiter_state.get("rate_limit_strikes", 0),
            "limiter_wait_seconds": float(self._limiter_snapshot.get("limiter_wait_seconds", 0.0)),
            "blocked_launches": int(self._limiter_snapshot.get("blocked_launches", 0)),
            "blocked_continuations": int(self._limiter_snapshot.get("blocked_continuations", 0)),
            "degrade_events": int(self._limiter_snapshot.get("degrade_events", 0)),
            "stage_name": stage_name,
        }
        self.write()

    def record_attempt_finished(
        self,
        *,
        stage_name: str,
        job: SwarmWorkerJob,
        finished_at_monotonic: float,
    ) -> None:
        stage = self._ensure_stage(stage_name)
        worker = self._ensure_worker(stage_name=stage_name, job=job)
        last_finished_at = datetime.now().isoformat(timespec="seconds")
        stage["last_finished_at"] = last_finished_at
        worker["last_finished_at"] = last_finished_at
        started_at = worker.get("_active_attempt_started_at")
        if started_at is None:
            self.write()
            return
        elapsed_seconds = max(0.0, finished_at_monotonic - float(started_at))
        stage["elapsed_seconds"] = round(stage["elapsed_seconds"] + elapsed_seconds, 6)
        worker["elapsed_seconds"] = round(worker["elapsed_seconds"] + elapsed_seconds, 6)
        worker["_active_attempt_started_at"] = None
        self.write()

    def mark_worker_completed(self, *, stage_name: str, job: SwarmWorkerJob) -> None:
        self._set_worker_status(stage_name=stage_name, job=job, status="completed")

    def mark_worker_failed(
        self,
        *,
        stage_name: str,
        job: SwarmWorkerJob,
        failure_message: str,
    ) -> None:
        self._set_worker_status(
            stage_name=stage_name,
            job=job,
            status="failed",
            failure_message=failure_message,
        )

    def mark_worker_skipped(
        self,
        *,
        stage_name: str,
        job: SwarmWorkerJob,
        failure_message: str,
    ) -> None:
        self._set_worker_status(
            stage_name=stage_name,
            job=job,
            status="skipped",
            failure_message=failure_message,
        )

    def mark_stage_aborted(self, *, stage_name: str, reason: str) -> None:
        stage = self._ensure_stage(stage_name)
        stage["aborted"] = True
        stage["abort_reason"] = reason
        self.write()

    def mark_failed(self, *, stage_name: str, reason: str) -> None:
        self.status = "failed"
        self.finished_at = datetime.now().isoformat(timespec="seconds")
        self.failure_stage = stage_name
        self.failure_reason = reason
        self._finished_monotonic = time.monotonic()
        self.write()

    def mark_completed(self) -> None:
        self.status = "completed"
        self.finished_at = datetime.now().isoformat(timespec="seconds")
        self.failure_stage = None
        self.failure_reason = None
        self._finished_monotonic = time.monotonic()
        self.write()

    def to_dict(self) -> dict[str, Any]:
        stages = {
            stage_name: {
                "worker_count": stage["worker_count"],
                "completed_workers": stage["completed_workers"],
                "failed_workers": stage["failed_workers"],
                "skipped_workers": stage["skipped_workers"],
                "retry_events": stage["retry_events"],
                "rate_limit_retry_events": stage["rate_limit_retry_events"],
                "limiter_wait_seconds": stage["limiter_wait_seconds"],
                "blocked_launches": stage["blocked_launches"],
                "blocked_continuations": stage["blocked_continuations"],
                "degrade_events": stage["degrade_events"],
                "elapsed_seconds": stage["elapsed_seconds"],
                "first_started_at": stage["first_started_at"],
                "last_finished_at": stage["last_finished_at"],
                "aborted": stage["aborted"],
                "abort_reason": stage["abort_reason"],
                "limiter": dict(stage["limiter"]),
                "totals": _public_usage_totals(stage["totals"]),
            }
            for stage_name, stage in sorted(self._stages.items())
        }
        workers = []
        for worker in sorted(
            self._workers.values(),
            key=lambda item: (str(item["stage"]), str(item["worker_id"])),
        ):
            workers.append(
                {
                    "stage": worker["stage"],
                    "worker_id": worker["worker_id"],
                    "worker_type": worker["worker_type"],
                    "lease_key": worker["lease_key"],
                    "model": worker["model"],
                    "reasoning_effort": worker["reasoning_effort"],
                    "estimated_request_tokens": worker["estimated_request_tokens"],
                    "status": worker["status"],
                    "attempts": worker["attempts"],
                    "retry_events": worker["retry_events"],
                    "rate_limit_retry_events": worker["rate_limit_retry_events"],
                    "degrade_events": worker["degrade_events"],
                    "elapsed_seconds": worker["elapsed_seconds"],
                    "first_started_at": worker["first_started_at"],
                    "last_finished_at": worker["last_finished_at"],
                    "last_retry_reason": worker["last_retry_reason"],
                    "last_retry_delay_seconds": worker["last_retry_delay_seconds"],
                    "failure_message": worker["failure_message"],
                    "totals": _public_usage_totals(worker["totals"]),
                }
            )
        wall_clock_seconds = self._wall_clock_seconds()
        avg_input_tpm = 0.0
        if wall_clock_seconds > 0:
            avg_input_tpm = round((self._totals["input_tokens"] / wall_clock_seconds) * 60.0, 6)
        top_token_workers = [
            {
                "stage": worker["stage"],
                "worker_id": worker["worker_id"],
                "lease_key": worker["lease_key"],
                "status": worker["status"],
                "input_tokens": worker["totals"]["input_tokens"],
                "peak_input_tokens": worker["totals"]["peak_input_tokens"],
            }
            for worker in sorted(
                self._workers.values(),
                key=lambda item: (
                    -_coerce_nonnegative_int(item["totals"]["input_tokens"]),
                    str(item["stage"]),
                    str(item["worker_id"]),
                ),
            )[:5]
        ]
        return {
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "wall_clock_seconds": wall_clock_seconds,
            "failure_stage": self.failure_stage,
            "failure_reason": self.failure_reason,
            "limiter": {
                **self._limiter_snapshot,
                "avg_input_tpm": avg_input_tpm,
                "top_token_workers": top_token_workers,
            },
            "totals": _public_usage_totals(self._totals),
            "stages": stages,
            "workers": workers,
        }

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    def _ensure_stage(self, stage_name: str) -> dict[str, Any]:
        stage = self._stages.get(stage_name)
        if stage is None:
            stage = {
                "worker_count": 0,
                "completed_workers": 0,
                "failed_workers": 0,
                "skipped_workers": 0,
                "retry_events": 0,
                "rate_limit_retry_events": 0,
                "elapsed_seconds": 0.0,
                "first_started_at": None,
                "last_finished_at": None,
                "aborted": False,
                "abort_reason": None,
                "limiter_wait_seconds": 0.0,
                "blocked_launches": 0,
                "blocked_continuations": 0,
                "degrade_events": 0,
                "limiter": {
                    "preset": None,
                    "hard_safe": True,
                    "model_limit_tpm": None,
                    "headroom_fraction": DEFAULT_SWARM_TPM_HEADROOM_FRACTION,
                    "current_stage_parallel_ceiling": 1,
                    "stage_degraded_after_rate_limit": False,
                    "rate_limit_strikes": 0,
                    "limiter_wait_seconds": 0.0,
                    "blocked_launches": 0,
                    "blocked_continuations": 0,
                    "degrade_events": 0,
                },
                "totals": _new_usage_totals(),
            }
            self._stages[stage_name] = stage
            self._stage_worker_keys[stage_name] = set()
        return stage

    def _ensure_worker(self, *, stage_name: str, job: SwarmWorkerJob) -> dict[str, Any]:
        stage = self._ensure_stage(stage_name)
        worker_key = f"{stage_name}:{job.worker_id}"
        worker = self._workers.get(worker_key)
        if worker is None:
            worker = {
                "stage": stage_name,
                "worker_id": job.worker_id,
                "worker_type": job.worker_type,
                "lease_key": job.lease_key,
                "model": job.model,
                "reasoning_effort": job.reasoning_effort,
                "estimated_request_tokens": _estimate_job_request_tokens(job),
                "status": "pending",
                "attempts": 0,
                "retry_events": 0,
                "rate_limit_retry_events": 0,
                "degrade_events": 0,
                "elapsed_seconds": 0.0,
                "first_started_at": None,
                "last_finished_at": None,
                "last_retry_reason": None,
                "last_retry_delay_seconds": None,
                "failure_message": None,
                "totals": _new_usage_totals(),
                "_active_attempt_started_at": None,
            }
            self._workers[worker_key] = worker
            self._stage_worker_keys[stage_name].add(worker_key)
            stage["worker_count"] = len(self._stage_worker_keys[stage_name])
        return worker

    def _set_worker_status(
        self,
        *,
        stage_name: str,
        job: SwarmWorkerJob,
        status: str,
        failure_message: str | None = None,
    ) -> None:
        stage = self._ensure_stage(stage_name)
        worker = self._ensure_worker(stage_name=stage_name, job=job)
        previous_status = worker["status"]
        if previous_status == "completed" and status != "completed":
            stage["completed_workers"] = max(0, stage["completed_workers"] - 1)
        if previous_status == "failed" and status != "failed":
            stage["failed_workers"] = max(0, stage["failed_workers"] - 1)
        if previous_status == "skipped" and status != "skipped":
            stage["skipped_workers"] = max(0, stage["skipped_workers"] - 1)
        if previous_status != "completed" and status == "completed":
            stage["completed_workers"] += 1
        if previous_status != "failed" and status == "failed":
            stage["failed_workers"] += 1
        if previous_status != "skipped" and status == "skipped":
            stage["skipped_workers"] += 1
        worker["status"] = status
        worker["failure_message"] = failure_message
        self.write()

    def _wall_clock_seconds(self) -> float:
        finished = self._finished_monotonic if self._finished_monotonic is not None else time.monotonic()
        return round(max(0.0, finished - self._started_monotonic), 6)


def _rate_limit_retry_delay_seconds(message: str | None) -> float | None:
    text = str(message or "").strip()
    if not text:
        return None
    lowered = text.lower()
    is_rate_limited = (
        "rate_limit_exceeded" in lowered
        or "rate limit reached" in lowered
        or "tokens per min" in lowered
        or " tpm" in lowered
    )
    if not is_rate_limited:
        return None
    match = RATE_LIMIT_RETRY_PATTERN.search(text)
    if match is not None:
        delay = float(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("ms"):
            delay /= 1000.0
        return max(delay, DEFAULT_SWARM_POLL_INTERVAL_SECONDS)
    return DEFAULT_SWARM_RATE_LIMIT_COOLDOWN_SECONDS


def _rate_limit_tpm_limit(message: str | None) -> int | None:
    text = str(message or "").strip()
    if not text:
        return None
    match = RATE_LIMIT_TPM_LIMIT_PATTERN.search(text)
    if match is None:
        return None
    try:
        parsed = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _rate_limit_retry_jitter_seconds(*, worker_id: str, retry_count: int) -> float:
    seed = f"{worker_id}:{retry_count}"
    fraction = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    return round(DEFAULT_SWARM_RATE_LIMIT_JITTER_SECONDS * (0.5 + fraction), 6)


class SwarmStageTokenLimiter:
    def __init__(
        self,
        *,
        cwd: Path,
        provider_name: str,
        stage_name: str,
        jobs: list[SwarmWorkerJob],
        configured_parallel_limit: int,
        preset: str,
        headroom_fraction: float = DEFAULT_SWARM_TPM_HEADROOM_FRACTION,
    ) -> None:
        self._cwd = cwd.resolve()
        self._provider_name = provider_name
        self._stage_name = stage_name
        self._preset = preset
        behavior = SWARM_STAGE_PRESET_BEHAVIORS.get(preset, SWARM_STAGE_PRESET_BEHAVIORS["safe"])
        self._hard_safe = bool(behavior["hard_safe"])
        bootstrap_parallel_limit = behavior["bootstrap_parallel_limit"]
        if bootstrap_parallel_limit is None:
            self._bootstrap_parallel_limit = max(1, configured_parallel_limit)
        else:
            self._bootstrap_parallel_limit = max(
                1,
                min(configured_parallel_limit, int(bootstrap_parallel_limit)),
            )
        self._rate_limit_strike_limit = int(behavior["rate_limit_strike_limit"])
        self._rate_limit_strikes = 0
        self._configured_parallel_limit = max(1, configured_parallel_limit)
        self._headroom_fraction = headroom_fraction
        self._current_stage_parallel_ceiling = self._bootstrap_parallel_limit
        self._stage_degraded_after_rate_limit = False
        self._saw_current_run_usage = False
        self._window_events_by_model: dict[str, list[tuple[float, int]]] = {}
        self._model_states: dict[str, dict[str, Any]] = {}
        ordered_models: list[str] = []
        for job in jobs:
            if job.model not in ordered_models:
                ordered_models.append(job.model)
        self._primary_model = ordered_models[0] if ordered_models else ""
        for model in ordered_models:
            record = load_learned_model_limit(cwd=self._cwd, provider=provider_name, model=model)
            self._model_states[model] = {
                "learned_tpm_limit": record.learned_tpm_limit if record is not None else None,
                "observed_peak_input_tokens": (
                    dict(record.observed_peak_input_tokens) if record is not None else {}
                ),
                "dirty": False,
            }
            self._window_events_by_model[model] = []

    @property
    def enabled(self) -> bool:
        return True

    @property
    def current_stage_parallel_ceiling(self) -> int:
        return self._current_stage_parallel_ceiling

    @property
    def stage_degraded_after_rate_limit(self) -> bool:
        return self._stage_degraded_after_rate_limit

    @property
    def headroom_fraction(self) -> float:
        return self._headroom_fraction

    @property
    def model_limit_tpm(self) -> int | None:
        if self._primary_model:
            return self._model_states[self._primary_model]["learned_tpm_limit"]
        return None

    def can_start_job(self, *, job: SwarmWorkerJob, active_jobs: tuple[SwarmWorkerJob, ...], now: float) -> bool:
        self._prune_window(job.model, now)
        if len(active_jobs) >= self._current_stage_parallel_ceiling:
            return False
        if not self._hard_safe:
            return True
        candidate_reservation = self._reservation_tokens(job)
        safe_limit = self._safe_limit(job.model)
        if candidate_reservation is None or safe_limit is None:
            return len(active_jobs) < self._bootstrap_parallel_limit
        active_reservations = 0
        for active_job in active_jobs:
            if active_job.model != job.model:
                continue
            reservation = self._reservation_tokens(active_job)
            if reservation is None:
                reservation = _estimate_job_request_tokens(active_job)
            active_reservations += reservation
        window_tokens = self._window_tokens(job.model, now)
        return window_tokens + active_reservations + candidate_reservation <= safe_limit

    def recommended_wait_seconds(
        self,
        *,
        job: SwarmWorkerJob,
        active_jobs: tuple[SwarmWorkerJob, ...],
        now: float,
    ) -> float:
        self._prune_window(job.model, now)
        if len(active_jobs) >= self._current_stage_parallel_ceiling:
            return 0.0
        if not self._hard_safe:
            return 0.0
        safe_limit = self._safe_limit(job.model)
        candidate_reservation = self._reservation_tokens(job)
        if safe_limit is None or candidate_reservation is None:
            return 0.0
        active_reservations = 0
        for active_job in active_jobs:
            if active_job.model != job.model:
                continue
            reservation = self._reservation_tokens(active_job)
            if reservation is None:
                reservation = _estimate_job_request_tokens(active_job)
            active_reservations += reservation
        remaining_capacity = safe_limit - active_reservations - candidate_reservation
        if remaining_capacity < 0:
            return 0.0
        window_events = sorted(self._window_events_by_model.get(job.model, []), key=lambda item: item[0])
        running_window_tokens = sum(tokens for _, tokens in window_events)
        if running_window_tokens <= remaining_capacity:
            return 0.0
        for timestamp, tokens in window_events:
            running_window_tokens -= tokens
            if running_window_tokens <= remaining_capacity:
                return max(0.0, DEFAULT_SWARM_TPM_WINDOW_SECONDS - (now - timestamp))
        return 0.0

    def record_provider_usage(self, *, job: SwarmWorkerJob, data: dict[str, Any], now: float) -> None:
        input_tokens = _coerce_nonnegative_int(data.get("input_tokens"))
        total_tokens = _coerce_nonnegative_int(data.get("total_tokens"))
        tokens_for_window = input_tokens or total_tokens
        if tokens_for_window > 0:
            self._window_events_by_model.setdefault(job.model, []).append((now, tokens_for_window))
            self._prune_window(job.model, now)
            self._saw_current_run_usage = True
        peak_input_tokens = input_tokens or total_tokens
        if peak_input_tokens > 0:
            state = self._model_states.setdefault(
                job.model,
                {
                    "learned_tpm_limit": None,
                    "observed_peak_input_tokens": {},
                    "dirty": False,
                },
            )
            previous_peak = _coerce_nonnegative_int(
                state["observed_peak_input_tokens"].get(job.worker_type)
            )
            if peak_input_tokens > previous_peak:
                state["observed_peak_input_tokens"][job.worker_type] = peak_input_tokens
                state["dirty"] = True
        self._recompute_parallel_ceiling()

    def record_rate_limit(self, *, job: SwarmWorkerJob, failure_message: str) -> None:
        self._rate_limit_strikes += 1
        state = self._model_states.setdefault(
            job.model,
            {
                "learned_tpm_limit": None,
                "observed_peak_input_tokens": {},
                "dirty": False,
            },
        )
        parsed_limit = _rate_limit_tpm_limit(failure_message)
        if parsed_limit is not None and parsed_limit != state["learned_tpm_limit"]:
            state["learned_tpm_limit"] = parsed_limit
            state["dirty"] = True
        if (
            self._rate_limit_strike_limit > 0
            and self._rate_limit_strikes >= self._rate_limit_strike_limit
        ):
            self._stage_degraded_after_rate_limit = True
        self._recompute_parallel_ceiling()

    def is_intrinsically_oversized(self, *, job: SwarmWorkerJob) -> bool:
        if not self._hard_safe:
            return False
        safe_limit = self._safe_limit(job.model)
        if safe_limit is None:
            return False
        candidate_reservation = self._reservation_tokens(job)
        if candidate_reservation is None:
            candidate_reservation = _estimate_job_request_tokens(job)
        return candidate_reservation > safe_limit

    def persist(self) -> None:
        for model, state in self._model_states.items():
            if not state.get("dirty"):
                continue
            save_learned_model_limit(
                cwd=self._cwd,
                provider=self._provider_name,
                model=model,
                learned_tpm_limit=state["learned_tpm_limit"],
                headroom_fraction=self._headroom_fraction,
                observed_peak_input_tokens=state["observed_peak_input_tokens"],
            )
            state["dirty"] = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "preset": self._preset,
            "hard_safe": self._hard_safe,
            "model_limit_tpm": self.model_limit_tpm,
            "headroom_fraction": self._headroom_fraction,
            "current_stage_parallel_ceiling": self._current_stage_parallel_ceiling,
            "stage_degraded_after_rate_limit": self._stage_degraded_after_rate_limit,
            "rate_limit_strikes": self._rate_limit_strikes,
            "stage_name": self._stage_name,
        }

    def _prune_window(self, model: str, now: float) -> None:
        retained: list[tuple[float, int]] = []
        for timestamp, tokens in self._window_events_by_model.get(model, []):
            if now - timestamp <= DEFAULT_SWARM_TPM_WINDOW_SECONDS:
                retained.append((timestamp, tokens))
        self._window_events_by_model[model] = retained

    def _window_tokens(self, model: str, now: float) -> int:
        self._prune_window(model, now)
        return sum(tokens for _, tokens in self._window_events_by_model.get(model, []))

    def _reservation_tokens(self, job: SwarmWorkerJob) -> int | None:
        state = self._model_states.get(job.model)
        if state is None:
            return None
        reservation = _coerce_nonnegative_int(state["observed_peak_input_tokens"].get(job.worker_type))
        return reservation or _estimate_job_request_tokens(job)

    def _safe_limit(self, model: str) -> int | None:
        state = self._model_states.get(model)
        if state is None or state["learned_tpm_limit"] is None:
            return None
        safe_limit = math.floor(int(state["learned_tpm_limit"]) * self._headroom_fraction)
        return safe_limit if safe_limit > 0 else None

    def _recompute_parallel_ceiling(self) -> None:
        if not self._hard_safe:
            self._current_stage_parallel_ceiling = self._configured_parallel_limit
            return
        if self._configured_parallel_limit <= 1 or self._stage_degraded_after_rate_limit:
            self._current_stage_parallel_ceiling = 1
            return
        if not self._saw_current_run_usage or self.model_limit_tpm is None:
            self._current_stage_parallel_ceiling = self._bootstrap_parallel_limit
            return
        self._current_stage_parallel_ceiling = self._configured_parallel_limit


def _build_seed_worker_jobs(
    *,
    cwd: Path,
    loaded,
    prompt_asset: SwarmPromptAsset,
    eligible_files: list[Path],
    swarm_digest_text: str,
    shared_manifest_text: str,
    seed_output_path: str,
    tool_schemas: tuple[dict[str, Any], ...],
) -> tuple[list[SwarmWorkerJob], list[tuple[str, str]]]:
    template = prompt_asset.read_text()
    jobs: list[SwarmWorkerJob] = []
    ordered_seed_metadata: list[tuple[str, str]] = []
    for index, target_path in enumerate(eligible_files, start=1):
        seed_id = f"SEED-{index:03d}"
        target_file = display_repo_path(cwd, target_path)
        ordered_seed_metadata.append((seed_id, target_file))
        target_size_bytes = target_path.stat().st_size
        compact_input = build_seed_input(
            seed_id=seed_id,
            target_file=target_file,
            target_size_bytes=target_size_bytes,
            swarm_digest_text=swarm_digest_text,
            shared_manifest_text=shared_manifest_text,
            context_level="compact",
        )
        minimal_input = build_seed_input(
            seed_id=seed_id,
            target_file=target_file,
            target_size_bytes=target_size_bytes,
            swarm_digest_text=swarm_digest_text,
            shared_manifest_text=shared_manifest_text,
            context_level="minimal",
        )
        jobs.append(
            SwarmWorkerJob(
                worker_id=seed_id,
                worker_type="seed_file",
                lease_key=f"file:{target_file}",
                model=loaded.effective.swarm.sweep_model,
                reasoning_effort=loaded.effective.swarm.reasoning.seed,
                instructions=render_seed_instructions(
                    template=template,
                    target_file=target_file,
                    output_path=seed_output_path,
                ),
                input_text=compact_input,
                prompt_cache_key=prompt_asset.prompt_cache_key,
                text_format=SEED_RESPONSE_FORMAT,
                tools=tool_schemas,
                progress_label=target_file,
                progress_action=f"inspect {target_file}",
                input_variants=(compact_input, minimal_input),
            )
        )
    return jobs, ordered_seed_metadata


def summarize_seed_request_volume(
    *,
    cwd: Path,
    loaded,
    prompt_bundle: SwarmPromptBundle,
    run_dir: Path,
    swarm_digest_path: Path,
    shared_manifest_path: Path,
    eligible_files: list[Path],
) -> SwarmSeedRequestVolume:
    swarm_digest_text = swarm_digest_path.read_text(encoding="utf-8")
    shared_manifest_text = shared_manifest_path.read_text(encoding="utf-8")
    seed_output_path = (run_dir / "swarm" / "seeds").relative_to(cwd).as_posix()
    jobs, ordered_seed_metadata = _build_seed_worker_jobs(
        cwd=cwd,
        loaded=loaded,
        prompt_asset=prompt_bundle.seed,
        eligible_files=eligible_files,
        swarm_digest_text=swarm_digest_text,
        shared_manifest_text=shared_manifest_text,
        seed_output_path=seed_output_path,
        tool_schemas=(),
    )
    estimates = [_estimate_job_request_tokens(job) for job in jobs]
    peak_count = max(1, loaded.effective.swarm.seed_max_parallel)
    peak_parallel_estimate = sum(sorted(estimates, reverse=True)[:peak_count])
    max_job_target_file = ""
    max_job_estimate = 0
    for estimate, (_, target_file) in zip(estimates, ordered_seed_metadata, strict=False):
        if estimate > max_job_estimate:
            max_job_estimate = estimate
            max_job_target_file = target_file
    return SwarmSeedRequestVolume(
        job_count=len(jobs),
        total_estimated_tokens=sum(estimates),
        peak_parallel_estimated_tokens=peak_parallel_estimate,
        max_job_estimated_tokens=max_job_estimate,
        max_job_target_file=max_job_target_file,
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


def parse_seed_payload(
    *,
    payload: dict[str, Any],
    seed_id: str,
    target_file: str,
) -> SwarmSeedResult:
    _require_payload_keys(
        payload,
        ("outcome", "severity_bucket", "claim", "evidence", "related_files", "notes"),
    )
    return normalize_seed_payload(payload=payload, seed_id=seed_id, target_file=target_file)


def build_proof_input(
    *,
    issue_candidate: SwarmIssueCandidate,
    issue_seed_results: tuple[SwarmSeedResult, ...],
    swarm_digest_text: str,
    shared_manifest_text: str,
) -> str:
    payload = {
        "task_type": "proof_issue",
        "case_id": issue_candidate.case_id,
        "lease_key": f"issue:{issue_candidate.case_id}",
        "shared_manifest_markdown": shared_manifest_text,
        "swarm_digest_markdown": swarm_digest_text,
        "issue_candidate": issue_candidate.to_dict(),
        "promoted_seeds": [result.to_dict() for result in issue_seed_results],
    }
    return json.dumps(payload, indent=2)


def normalize_proof_payload(
    *,
    payload: dict[str, Any],
    issue_candidate: SwarmIssueCandidate,
) -> SwarmProofResult:
    proof_state = str(payload.get("proof_state", "") or "").strip().lower()
    if proof_state not in PROOF_STATE_VALUES:
        proof_state = "written_proof" if _string_list(payload.get("repro_steps")) else "hypothesized"

    outcome = str(payload.get("outcome", "") or "").strip().lower()
    if outcome not in {"reportable", "not_reportable"}:
        if proof_state in REPORTABLE_PROOF_STATES:
            outcome = "reportable"
        else:
            outcome = "not_reportable"

    filter_reason = str(payload.get("filter_reason", "") or "").strip()
    if outcome == "reportable":
        filter_reason = ""
    elif not filter_reason:
        filter_reason = "insufficient proof for final report"

    claim = str(payload.get("claim", "") or "").strip() or issue_candidate.claim
    summary = str(payload.get("summary", "") or "").strip()
    notes = tuple(_string_list(payload.get("notes")))

    contradiction_phrase = _reportable_contradiction(summary=summary, notes=notes)
    if outcome == "reportable" and contradiction_phrase is not None:
        outcome = "not_reportable"
        proof_state = "path_grounded"
        filter_reason = f"proof summary contradicts reportable outcome: {contradiction_phrase}"

    return SwarmProofResult(
        case_id=issue_candidate.case_id,
        primary_seed_id=issue_candidate.primary_seed_id,
        primary_target_file=issue_candidate.primary_target_file,
        severity_bucket=issue_candidate.severity_bucket,
        seed_ids=issue_candidate.seed_ids,
        duplicate_seed_ids=issue_candidate.duplicate_seed_ids,
        outcome=outcome,
        proof_state=proof_state,
        claim=claim,
        summary=summary,
        preconditions=tuple(_string_list(payload.get("preconditions"))),
        repro_steps=tuple(_string_list(payload.get("repro_steps"))),
        citations=tuple(_string_list(payload.get("citations"))),
        notes=notes,
        filter_reason=filter_reason,
    )


def parse_proof_payload(
    *,
    payload: dict[str, Any],
    issue_candidate: SwarmIssueCandidate,
) -> SwarmProofResult:
    _require_payload_keys(
        payload,
        (
            "outcome",
            "proof_state",
            "claim",
            "summary",
            "preconditions",
            "repro_steps",
            "citations",
            "notes",
            "filter_reason",
        ),
    )
    return normalize_proof_payload(payload=payload, issue_candidate=issue_candidate)


def promote_issue_candidates(seed_results: list[SwarmSeedResult]) -> tuple[SwarmIssueCandidate, ...]:
    promoted = [result for result in seed_results if result.outcome == "finding"]
    if not promoted:
        return ()

    seed_lookup = {result.seed_id: result for result in promoted}
    adjacency: dict[str, set[str]] = {result.seed_id: set() for result in promoted}
    grouping_keys_by_seed: dict[str, set[str]] = {result.seed_id: set() for result in promoted}

    for index, left in enumerate(promoted):
        for right in promoted[index + 1 :]:
            grouping_keys = _issue_grouping_keys(left, right)
            if not grouping_keys:
                continue
            adjacency[left.seed_id].add(right.seed_id)
            adjacency[right.seed_id].add(left.seed_id)
            grouping_keys_by_seed[left.seed_id].update(grouping_keys)
            grouping_keys_by_seed[right.seed_id].update(grouping_keys)

    ordered_promoted = sorted(
        promoted,
        key=lambda item: (_severity_rank(item.severity_bucket), item.seed_id),
    )

    issue_candidates: list[SwarmIssueCandidate] = []
    visited: set[str] = set()
    for result in ordered_promoted:
        if result.seed_id in visited:
            continue
        pending = [result.seed_id]
        component_ids: list[str] = []
        while pending:
            seed_id = pending.pop()
            if seed_id in visited:
                continue
            visited.add(seed_id)
            component_ids.append(seed_id)
            pending.extend(sorted(adjacency[seed_id] - visited))

        component = sorted(
            (seed_lookup[seed_id] for seed_id in component_ids),
            key=lambda item: (_severity_rank(item.severity_bucket), item.seed_id),
        )
        primary = component[0]
        case_id = f"SWM-{len(issue_candidates) + 1:03d}"
        issue_candidates.append(
            SwarmIssueCandidate(
                case_id=case_id,
                primary_seed_id=primary.seed_id,
                primary_target_file=primary.target_file,
                severity_bucket=primary.severity_bucket,
                claim=primary.claim,
                evidence=primary.evidence,
                related_files=primary.related_files,
                notes=primary.notes,
                seed_ids=tuple(item.seed_id for item in component),
                duplicate_seed_ids=tuple(
                    item.seed_id for item in component if item.seed_id != primary.seed_id
                ),
                target_files=tuple(item.target_file for item in component),
                grouping_keys=tuple(
                    sorted(
                        {
                            key
                            for item in component
                            for key in grouping_keys_by_seed[item.seed_id]
                        }
                    )
                ),
            )
        )

    return tuple(issue_candidates)


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


def write_case_groups(
    path: Path,
    issue_candidates: tuple[SwarmIssueCandidate, ...],
    seed_results: list[SwarmSeedResult],
    proof_results: tuple[SwarmProofResult, ...],
) -> None:
    lines = ["# Case groups", ""]
    if not issue_candidates:
        lines.append("No seed findings were promoted into issue candidates.")
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return

    seed_lookup = {result.seed_id: result for result in seed_results}
    proof_lookup = {result.case_id: result for result in proof_results}
    for issue_candidate in issue_candidates:
        proof_result = proof_lookup.get(issue_candidate.case_id)
        lines.extend(
            [
                f"## {issue_candidate.case_id}",
                f"- Primary seed: `{issue_candidate.primary_seed_id}`",
                f"- Severity: `{issue_candidate.severity_bucket}`",
                f"- Primary file: `{issue_candidate.primary_target_file}`",
                f"- Seed count: `{len(issue_candidate.seed_ids)}`",
                f"- Claim: {issue_candidate.claim}",
            ]
        )
        if issue_candidate.duplicate_seed_ids:
            lines.append(
                "- Duplicate seeds: "
                + ", ".join(f"`{seed_id}`" for seed_id in issue_candidate.duplicate_seed_ids)
            )
        if issue_candidate.grouping_keys:
            lines.append(
                "- Grouped via: "
                + ", ".join(f"`{item}`" for item in issue_candidate.grouping_keys)
            )
        if proof_result is not None:
            lines.append(f"- Proof state: `{proof_result.proof_state}`")
            lines.append(f"- Final outcome: `{proof_result.outcome}`")
        lines.extend(["", "### Member seeds"])
        for seed_id in issue_candidate.seed_ids:
            seed_result = seed_lookup[seed_id]
            lines.extend(
                [
                    f"- `{seed_result.seed_id}`",
                    f"  target=`{seed_result.target_file}` severity=`{seed_result.severity_bucket}`",
                    f"  claim={seed_result.claim or '(none)'}",
                ]
            )
        lines.append("")
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def write_final_ranked_findings(path: Path, proof_results: tuple[SwarmProofResult, ...]) -> None:
    findings = sorted(
        [result for result in proof_results if result.meets_report_bar],
        key=lambda item: (
            _severity_rank(item.severity_bucket),
            _proof_state_rank(item.proof_state),
            item.primary_target_file,
        ),
    )
    filtered = sorted(
        [result for result in proof_results if not result.meets_report_bar],
        key=lambda item: (
            _severity_rank(item.severity_bucket),
            _proof_state_rank(item.proof_state),
            item.primary_target_file,
        ),
    )
    lines = [
        "# Final ranked findings",
        "",
    ]
    if not findings:
        lines.append("No findings cleared the proof-stage report bar.")
    for index, result in enumerate(findings, start=1):
        lines.extend(
            [
                f"## {index}. {result.primary_seed_id}",
                f"- Case: `{result.case_id}`",
                f"- Proof state: `{result.proof_state}`",
                f"- Severity: `{result.severity_bucket}`",
                f"- Primary file: `{result.primary_target_file}`",
                f"- Claim: {result.claim or '(none)'}",
            ]
        )
        if result.duplicate_seed_ids:
            lines.append(
                "- Related duplicate seeds: "
                + ", ".join(f"`{item}`" for item in result.duplicate_seed_ids)
            )
        if result.summary:
            lines.append(f"- Proof summary: {result.summary}")
        if result.citations:
            lines.append("- Citations:")
            for item in result.citations:
                lines.append(f"  - {item}")
        if result.repro_steps:
            lines.append("- Repro steps:")
            for item in result.repro_steps:
                lines.append(f"  - {item}")
        if result.notes:
            lines.append("- Notes:")
            for item in result.notes:
                lines.append(f"  - {item}")
        lines.append("")
    if filtered:
        lines.extend(["## Filtered out", ""])
        for result in filtered:
            lines.append(
                f"- `{result.primary_seed_id}` case=`{result.case_id}` state=`{result.proof_state}` reason={result.filter_reason or '(not provided)'}"
            )
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def write_final_summary(
    path: Path,
    seed_results: list[SwarmSeedResult],
    issue_candidates: tuple[SwarmIssueCandidate, ...],
    proof_results: tuple[SwarmProofResult, ...],
    seed_ledger: Path,
    case_groups: Path,
    final_ranked_findings: Path,
    usage_summary: Path | None = None,
) -> None:
    findings = [result for result in proof_results if result.meets_report_bar]
    filtered = [result for result in proof_results if not result.meets_report_bar]
    proof_state_counts = {
        state: sum(1 for result in proof_results if result.proof_state == state)
        for state in PROOF_STATE_VALUES
    }
    lines = [
        "# Final summary",
        "",
        f"- Eligible files processed: `{len(seed_results)}`",
        f"- Seed findings surfaced: `{sum(1 for result in seed_results if result.outcome == 'finding')}`",
        f"- Promoted issue candidates: `{len(issue_candidates)}`",
        f"- Findings kept after proof: `{len(findings)}`",
        f"- Filtered after proof: `{len(filtered)}`",
        "- Proof stage mode: `read-only validation`",
        f"- Written proofs: `{proof_state_counts['written_proof']}`",
        f"- Path-grounded only: `{proof_state_counts['path_grounded']}`",
        f"- Hypothesized only: `{proof_state_counts['hypothesized']}`",
        f"- Seed ledger: `{seed_ledger}`",
        f"- Case groups: `{case_groups}`",
        f"- Ranked findings: `{final_ranked_findings}`",
    ]
    if usage_summary is not None:
        lines.append(f"- Usage summary: `{usage_summary}`")
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def write_partial_summary(
    path: Path,
    *,
    stage_name: str,
    failure: SwarmWorkerFailureDiagnostic | None,
    completed_worker_ids: tuple[str, ...],
    skipped_worker_ids: tuple[str, ...],
    seed_results: tuple[SwarmSeedResult, ...],
    issue_candidates: tuple[SwarmIssueCandidate, ...],
    proof_results: tuple[SwarmProofResult, ...],
    usage_summary: Path | None = None,
    seed_ledger: Path | None = None,
) -> None:
    lines = [
        "# Partial summary",
        "",
        f"- Stage aborted: `{stage_name}`",
        f"- Completed workers: `{len(completed_worker_ids)}`",
        f"- Skipped workers: `{len(skipped_worker_ids)}`",
        f"- Completed seed artifacts: `{len(seed_results)}`",
        f"- Promoted issue candidates: `{len(issue_candidates)}`",
        f"- Completed proof artifacts: `{len(proof_results)}`",
    ]
    if failure is not None:
        lines.append(f"- Abort reason: {failure.render_summary()}")
    if usage_summary is not None:
        lines.append(f"- Usage summary: `{usage_summary}`")
    if seed_ledger is not None:
        lines.append(f"- Seed ledger: `{seed_ledger}`")
    if completed_worker_ids:
        lines.extend(["", "## Completed workers"])
        for worker_id in completed_worker_ids:
            lines.append(f"- `{worker_id}`")
    if skipped_worker_ids:
        lines.extend(["", "## Skipped workers"])
        for worker_id in skipped_worker_ids:
            lines.append(f"- `{worker_id}`")
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


def _write_proof_artifacts(proofs_dir: Path, proof_result: SwarmProofResult) -> None:
    proof_prefix = proof_result.case_id.lower().replace("-", "_")
    json_path = proofs_dir / f"{proof_prefix}.json"
    md_path = proofs_dir / f"{proof_prefix}.md"
    json_path.write_text(json.dumps(proof_result.to_dict(), indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_proof_markdown(proof_result), encoding="utf-8")


def render_proof_markdown(proof_result: SwarmProofResult) -> str:
    lines = [
        f"# {proof_result.case_id}",
        "",
        f"- Primary seed: `{proof_result.primary_seed_id}`",
        f"- Primary file: `{proof_result.primary_target_file}`",
        f"- Severity: `{proof_result.severity_bucket}`",
        f"- Outcome: `{proof_result.outcome}`",
        f"- Proof state: `{proof_result.proof_state}`",
    ]
    if proof_result.duplicate_seed_ids:
        lines.append(
            "- Duplicate seeds: "
            + ", ".join(f"`{seed_id}`" for seed_id in proof_result.duplicate_seed_ids)
        )
    if proof_result.claim:
        lines.extend(["", "## Claim", proof_result.claim])
    if proof_result.summary:
        lines.extend(["", "## Proof summary", proof_result.summary])
    if proof_result.preconditions:
        lines.extend(["", "## Preconditions"])
        for item in proof_result.preconditions:
            lines.append(f"- {item}")
    if proof_result.repro_steps:
        lines.extend(["", "## Repro steps"])
        for item in proof_result.repro_steps:
            lines.append(f"- {item}")
    if proof_result.citations:
        lines.extend(["", "## Citations"])
        for item in proof_result.citations:
            lines.append(f"- {item}")
    if proof_result.notes:
        lines.extend(["", "## Notes"])
        for item in proof_result.notes:
            lines.append(f"- {item}")
    if proof_result.filter_reason:
        lines.extend(["", "## Filter reason", proof_result.filter_reason])
    return "\n".join(lines).strip() + "\n"


def _with_line_numbers(text: str, *, start_line: int = 1) -> str:
    lines = text.splitlines()
    if text.endswith("\n"):
        lines.append("")
    if not lines:
        return f"{start_line:>5} | "
    return "\n".join(
        f"{index:>5} | {line}" for index, line in enumerate(lines, start=start_line)
    )


def _require_payload_keys(payload: dict[str, Any], keys: tuple[str, ...]) -> None:
    missing = [key for key in keys if key not in payload]
    if missing:
        raise RuntimeError("Structured swarm response missing keys: " + ", ".join(missing))


def _issue_grouping_keys(left: SwarmSeedResult, right: SwarmSeedResult) -> tuple[str, ...]:
    left_supporting = _supporting_context_files(left)
    right_supporting = _supporting_context_files(right)
    keys: set[str] = set()

    # Merge duplicate seeds only when they explicitly point at one another's target files.
    if left.target_file in right_supporting:
        keys.add(left.target_file)
    if right.target_file in left_supporting:
        keys.add(right.target_file)
    if len(keys) < 2:
        return ()
    return tuple(sorted(keys))


def _supporting_context_files(seed_result: SwarmSeedResult) -> set[str]:
    return set(seed_result.related_files) | set(_evidence_file_refs(seed_result.evidence))


def _evidence_file_refs(evidence: tuple[str, ...]) -> tuple[str, ...]:
    refs: list[str] = []
    for item in evidence:
        raw_item = str(item).strip().strip("`")
        if not raw_item:
            continue
        candidate = raw_item.split(":", 1)[0].strip()
        if not candidate:
            continue
        try:
            normalized = _normalize_repo_relative_path(candidate)
        except RuntimeError:
            continue
        if normalized not in refs:
            refs.append(normalized)
    return tuple(refs)


def list_repo_file_entries(cwd: Path) -> list[RepoFileEntry]:
    repo_dir = cwd.resolve()
    git_entries = _git_tracked_files(repo_dir)
    if git_entries is not None:
        return git_entries

    files: list[RepoFileEntry] = []
    for path in sorted(repo_dir.rglob("*")):
        if path.is_symlink():
            continue
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
    prompt_bundle: SwarmPromptBundle,
    run_dir: Path,
    swarm_digest_path: Path,
    shared_manifest_path: Path,
    eligible_files: list[Path],
    progress_callback: SwarmProgressCallback | None = None,
) -> SwarmSweepResult:
    if loaded.effective.swarm is None:
        raise RuntimeError("Swarm config is not available.")

    swarm_root = run_dir / "swarm"
    seeds_dir = swarm_root / "seeds"
    proofs_dir = swarm_root / "proofs"
    reports_dir = swarm_root / "reports"
    usage_summary = swarm_root / "usage_summary.json"
    tool_trace_log = swarm_root / "tool_trace.jsonl"
    seeds_dir.mkdir(parents=True, exist_ok=True)
    proofs_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    swarm_digest_text = swarm_digest_path.read_text(encoding="utf-8")
    shared_manifest_text = shared_manifest_path.read_text(encoding="utf-8")
    tools = RepoReadOnlyTools(
        cwd=cwd,
        scope_include=loaded.effective.scope.include,
        scope_exclude=loaded.effective.scope.exclude,
        extra_allowed_paths=_staged_shared_resource_files(shared_manifest_path),
    )
    metrics = SwarmRunMetrics(path=usage_summary)
    metrics.write()
    prompt_asset = prompt_bundle.seed
    seed_output_path = seeds_dir.relative_to(cwd).as_posix()
    jobs, ordered_seed_metadata = _build_seed_worker_jobs(
        cwd=cwd,
        loaded=loaded,
        prompt_asset=prompt_asset,
        eligible_files=eligible_files,
        swarm_digest_text=swarm_digest_text,
        shared_manifest_text=shared_manifest_text,
        seed_output_path=seed_output_path,
        tool_schemas=tuple(tools.schemas()),
    )
    seed_results_by_id: dict[str, SwarmSeedResult] = {}
    target_files_by_seed_id = dict(ordered_seed_metadata)
    current_stage = "seed"
    seed_ledger = reports_dir / "seed_ledger.md"
    case_groups = reports_dir / "case_groups.md"
    final_ranked_findings = reports_dir / "final_ranked_findings.md"
    final_summary = reports_dir / "final_summary.md"
    partial_summary = reports_dir / "partial_summary.md"
    seed_results: tuple[SwarmSeedResult, ...] = ()
    issue_candidates: tuple[SwarmIssueCandidate, ...] = ()
    proof_results: tuple[SwarmProofResult, ...] = ()

    def _persist_seed_result(job: SwarmWorkerJob, result: ProviderTurnResult) -> None:
        seed_result = parse_seed_payload(
            payload=_parse_json_object(result.final_text),
            seed_id=job.worker_id,
            target_file=target_files_by_seed_id[job.worker_id],
        )
        _write_seed_artifacts(seeds_dir, seed_result)
        seed_results_by_id[job.worker_id] = seed_result

    try:
        try:
            provider_results = run_background_swarm_workers(
                provider=provider,
                jobs=jobs,
                tool_executor=tools.run,
                max_parallel=loaded.effective.swarm.seed_max_parallel,
                max_retries=DEFAULT_SWARM_MAX_RETRIES,
                rate_limit_max_retries=loaded.effective.swarm.rate_limit_max_retries,
                max_inflight_estimated_tokens=(
                    None
                    if loaded.effective.swarm.budget_mode == "advisory"
                    else loaded.effective.swarm.token_budget
                ),
                on_worker_completed=_persist_seed_result,
                stage_name="seed",
                metrics_tracker=metrics,
                progress_callback=progress_callback,
                cwd=cwd,
                provider_name=loaded.effective.active_provider,
                scheduler_preset=loaded.effective.swarm.preset,
                tool_trace_path=tool_trace_log,
            )
            resolved_seed_results: list[SwarmSeedResult] = []
            for seed_id, target_file in ordered_seed_metadata:
                seed_result = seed_results_by_id.get(seed_id)
                if seed_result is None:
                    result = provider_results[seed_id]
                    payload = _parse_json_object(result.final_text)
                    seed_result = parse_seed_payload(
                        payload=payload,
                        seed_id=seed_id,
                        target_file=target_file,
                    )
                    _write_seed_artifacts(seeds_dir, seed_result)
                resolved_seed_results.append(seed_result)
            seed_results = tuple(resolved_seed_results)
        except SwarmStageAbort as exc:
            seed_results = tuple(
                seed_results_by_id[seed_id]
                for seed_id, _ in ordered_seed_metadata
                if seed_id in seed_results_by_id
            )
            write_seed_ledger(seed_ledger, list(seed_results))
            write_partial_summary(
                partial_summary,
                stage_name="seed",
                failure=exc.primary_diagnostic,
                completed_worker_ids=tuple(result.seed_id for result in seed_results),
                skipped_worker_ids=exc.skipped_worker_ids,
                seed_results=seed_results,
                issue_candidates=(),
                proof_results=(),
                usage_summary=usage_summary,
                seed_ledger=seed_ledger,
            )
            raise

        write_seed_ledger(seed_ledger, list(seed_results))
        issue_candidates = promote_issue_candidates(list(seed_results))
        proof_results_list: list[SwarmProofResult] = []
        current_stage = "proof"
        if issue_candidates:
            proof_prompt_asset = prompt_bundle.proof
            seed_lookup = {result.seed_id: result for result in seed_results}
            proof_jobs: list[SwarmWorkerJob] = []
            issue_candidates_by_case_id: dict[str, SwarmIssueCandidate] = {}
            for issue_candidate in issue_candidates:
                issue_candidates_by_case_id[issue_candidate.case_id] = issue_candidate
                issue_seed_results = tuple(seed_lookup[seed_id] for seed_id in issue_candidate.seed_ids)
                proof_jobs.append(
                    SwarmWorkerJob(
                        worker_id=issue_candidate.case_id,
                        worker_type="proof_issue",
                        lease_key=f"issue:{issue_candidate.case_id}",
                        model=loaded.effective.swarm.proof_model,
                        reasoning_effort=loaded.effective.swarm.reasoning.proof,
                        instructions=proof_prompt_asset.read_text(),
                        input_text=build_proof_input(
                            issue_candidate=issue_candidate,
                            issue_seed_results=issue_seed_results,
                            swarm_digest_text=swarm_digest_text,
                            shared_manifest_text=shared_manifest_text,
                        ),
                        prompt_cache_key=proof_prompt_asset.prompt_cache_key,
                        text_format=PROOF_RESPONSE_FORMAT,
                        tools=tuple(tools.schemas()),
                        progress_label=issue_candidate.primary_target_file,
                        progress_action=(
                            f"validate promoted finding for {issue_candidate.primary_target_file}"
                        ),
                    )
                )
            proof_results_by_case_id: dict[str, SwarmProofResult] = {}

            def _persist_proof_result(job: SwarmWorkerJob, result: ProviderTurnResult) -> None:
                proof_result = parse_proof_payload(
                    payload=_parse_json_object(result.final_text),
                    issue_candidate=issue_candidates_by_case_id[job.worker_id],
                )
                _write_proof_artifacts(proofs_dir, proof_result)
                proof_results_by_case_id[job.worker_id] = proof_result

            try:
                proof_provider_results = run_background_swarm_workers(
                    provider=provider,
                    jobs=proof_jobs,
                    tool_executor=tools.run,
                    max_parallel=loaded.effective.swarm.proof_max_parallel,
                    max_retries=DEFAULT_SWARM_MAX_RETRIES,
                    rate_limit_max_retries=loaded.effective.swarm.rate_limit_max_retries,
                    max_inflight_estimated_tokens=(
                        None
                        if loaded.effective.swarm.budget_mode == "advisory"
                        else loaded.effective.swarm.token_budget
                    ),
                    on_worker_completed=_persist_proof_result,
                    stage_name="proof",
                    metrics_tracker=metrics,
                    progress_callback=progress_callback,
                    cwd=cwd,
                    provider_name=loaded.effective.active_provider,
                    scheduler_preset=loaded.effective.swarm.preset,
                    tool_trace_path=tool_trace_log,
                )
                for issue_candidate in issue_candidates:
                    proof_result = proof_results_by_case_id.get(issue_candidate.case_id)
                    if proof_result is None:
                        result = proof_provider_results[issue_candidate.case_id]
                        payload = _parse_json_object(result.final_text)
                        proof_result = parse_proof_payload(
                            payload=payload,
                            issue_candidate=issue_candidate,
                        )
                        _write_proof_artifacts(proofs_dir, proof_result)
                    proof_results_list.append(proof_result)
            except SwarmStageAbort as exc:
                proof_results = tuple(
                    proof_results_by_case_id[issue_candidate.case_id]
                    for issue_candidate in issue_candidates
                    if issue_candidate.case_id in proof_results_by_case_id
                )
                write_partial_summary(
                    partial_summary,
                    stage_name="proof",
                    failure=exc.primary_diagnostic,
                    completed_worker_ids=tuple(result.case_id for result in proof_results),
                    skipped_worker_ids=exc.skipped_worker_ids,
                    seed_results=seed_results,
                    issue_candidates=issue_candidates,
                    proof_results=proof_results,
                    usage_summary=usage_summary,
                    seed_ledger=seed_ledger,
                )
                raise
        proof_results = tuple(proof_results_list)

        current_stage = "report"
        write_case_groups(case_groups, issue_candidates, list(seed_results), proof_results)
        write_final_ranked_findings(final_ranked_findings, proof_results)
        write_final_summary(
            final_summary,
            list(seed_results),
            issue_candidates,
            proof_results,
            seed_ledger,
            case_groups,
            final_ranked_findings,
            usage_summary,
        )
    except Exception as exc:
        metrics.mark_failed(stage_name=current_stage, reason=str(exc))
        raise

    metrics.mark_completed()
    return SwarmSweepResult(
        seeds_dir=seeds_dir,
        proofs_dir=proofs_dir,
        reports_dir=reports_dir,
        tool_trace_log=tool_trace_log,
        seed_results=seed_results,
        issue_candidates=issue_candidates,
        proof_results=proof_results,
        seed_ledger=seed_ledger,
        case_groups=case_groups,
        final_ranked_findings=final_ranked_findings,
        final_summary=final_summary,
        usage_summary=usage_summary,
    )


def run_background_swarm_workers(
    *,
    provider: OpenAIResponsesProvider,
    jobs: list[SwarmWorkerJob],
    tool_executor,
    stage_name: str = "swarm",
    max_parallel: int | None = None,
    poll_interval_seconds: float = DEFAULT_SWARM_POLL_INTERVAL_SECONDS,
    max_retries: int = DEFAULT_SWARM_MAX_RETRIES,
    rate_limit_max_retries: int = DEFAULT_SWARM_MAX_RETRIES,
    max_inflight_estimated_tokens: int | None = None,
    on_worker_completed: Callable[[SwarmWorkerJob, ProviderTurnResult], None] | None = None,
    metrics_tracker: SwarmRunMetrics | None = None,
    progress_callback: SwarmProgressCallback | None = None,
    cwd: Path | None = None,
    provider_name: str | None = None,
    scheduler_preset: str = "safe",
    tool_trace_path: Path | None = None,
) -> dict[str, ProviderTurnResult]:
    if not jobs:
        return {}
    if metrics_tracker is not None:
        metrics_tracker.register_stage_jobs(stage_name=stage_name, jobs=jobs)

    distinct_lease_count = len({job.lease_key for job in jobs})
    configured_parallel_limit = max_parallel or min(DEFAULT_SWARM_MAX_PARALLEL, max(1, distinct_lease_count))
    limiter = None
    if cwd is not None and provider_name:
        limiter = SwarmStageTokenLimiter(
            cwd=cwd,
            provider_name=provider_name,
            stage_name=stage_name,
            jobs=jobs,
            configured_parallel_limit=configured_parallel_limit,
            preset=scheduler_preset,
        )
        if metrics_tracker is not None:
            metrics_tracker.update_limiter_state(stage_name=stage_name, limiter_state=limiter.snapshot())
    _emit_swarm_progress(
        progress_callback,
        "stage_started",
        stage_name=stage_name,
        worker_count=len(jobs),
        max_parallel=configured_parallel_limit,
    )

    estimated_tokens_by_job_id = {
        job.worker_id: _estimate_job_request_tokens(job) for job in jobs
    }
    job_order_by_id = {job.worker_id: index for index, job in enumerate(jobs)}
    pending: list[SwarmWorkerJob] = sorted(
        list(jobs),
        key=lambda item: (
            estimated_tokens_by_job_id[item.worker_id],
            job_order_by_id[item.worker_id],
        ),
    )
    active: dict[str, ActiveSwarmWorker] = {}
    awaiting_continuation: dict[str, ActiveSwarmWorker] = {}
    active_leases: set[str] = set()
    retry_counts: dict[str, int] = {job.worker_id: 0 for job in jobs}
    rate_limit_retry_counts: dict[str, int] = {job.worker_id: 0 for job in jobs}
    attempt_counts: dict[str, int] = {job.worker_id: 0 for job in jobs}
    results: dict[str, ProviderTurnResult] = {}
    cooldown_until = 0.0
    abort_diagnostics: list[SwarmWorkerFailureDiagnostic] = []
    aborting_due_to_rate_limit = False

    try:
        while pending or active or awaiting_continuation:
            permanent_failures: list[SwarmWorkerFailureDiagnostic] = []
            blocked_job: SwarmWorkerJob | None = None
            blocked_continuation = False
            while not aborting_due_to_rate_limit and len(active) < configured_parallel_limit:
                scheduled_this_pass = False
                hit_cooldown = False
                now = time.monotonic()
                if cooldown_until > now:
                    break
                active_jobs = tuple(state.job for state in active.values())
                active_estimated_tokens = sum(
                    estimated_tokens_by_job_id[state.job.worker_id] for state in active.values()
                )

                scheduled_continuation = False
                for worker_id, state in list(awaiting_continuation.items()):
                    job = state.job
                    if limiter is not None:
                        if metrics_tracker is not None:
                            metrics_tracker.update_limiter_state(
                                stage_name=stage_name,
                                limiter_state=limiter.snapshot(),
                            )
                        if not limiter.can_start_job(job=job, active_jobs=active_jobs, now=now):
                            blocked_job = job
                            blocked_continuation = True
                            continue
                    job_estimated_tokens = estimated_tokens_by_job_id[job.worker_id]
                    if (
                        max_inflight_estimated_tokens is not None
                        and active
                        and active_estimated_tokens + job_estimated_tokens > max_inflight_estimated_tokens
                    ):
                        blocked_job = job
                        blocked_continuation = True
                        continue
                    try:
                        handle = provider.continue_background_turn(
                            previous_response_id=state.pending_continuation_response_id or "",
                            model=job.model,
                            input_items=state.pending_continuation_input,
                            tools=job.tools,
                            text_format=job.text_format,
                        )
                    except Exception as exc:
                        failure_message = provider.classify_provider_failure(exc) or str(exc)
                        rate_limit_delay = _rate_limit_retry_delay_seconds(failure_message)
                        if rate_limit_delay is not None:
                            rate_limit_retry_counts[job.worker_id] += 1
                            if limiter is not None:
                                limiter.record_rate_limit(job=job, failure_message=failure_message)
                                if metrics_tracker is not None:
                                    metrics_tracker.update_limiter_state(
                                        stage_name=stage_name,
                                        limiter_state=limiter.snapshot(),
                                    )
                            retry_delay = rate_limit_delay + _rate_limit_retry_jitter_seconds(
                                worker_id=job.worker_id,
                                retry_count=rate_limit_retry_counts[job.worker_id],
                            )
                            if rate_limit_retry_counts[job.worker_id] <= rate_limit_max_retries:
                                cooldown_until = max(cooldown_until, time.monotonic() + retry_delay)
                                if metrics_tracker is not None:
                                    metrics_tracker.record_retry(
                                        stage_name=stage_name,
                                        job=job,
                                        reason="rate_limit",
                                        delay_seconds=retry_delay,
                                    )
                                _emit_swarm_progress(
                                    progress_callback,
                                    "worker_retry",
                                    reason="rate_limit",
                                    delay_seconds=retry_delay,
                                    failure_message=failure_message,
                                    **_swarm_progress_payload(stage_name, job),
                                )
                                blocked_job = job
                                blocked_continuation = True
                                hit_cooldown = True
                                break
                            awaiting_continuation.pop(worker_id, None)
                            active_leases.discard(job.lease_key)
                            diagnostic = SwarmWorkerFailureDiagnostic(
                                stage=stage_name,
                                worker_id=job.worker_id,
                                lease_key=job.lease_key,
                                failure_message=failure_message or "rate-limit retries exhausted",
                                response_id=state.pending_continuation_response_id,
                            )
                            abort_diagnostics.append(diagnostic)
                            aborting_due_to_rate_limit = True
                            if metrics_tracker is not None:
                                metrics_tracker.mark_worker_failed(
                                    stage_name=stage_name,
                                    job=job,
                                    failure_message=diagnostic.failure_message,
                                )
                                metrics_tracker.mark_stage_aborted(
                                    stage_name=stage_name,
                                    reason=diagnostic.failure_message,
                                )
                            _emit_swarm_progress(
                                progress_callback,
                                "worker_failed",
                                failure_message=diagnostic.failure_message,
                                response_id=diagnostic.response_id,
                                **_swarm_progress_payload(stage_name, job),
                            )
                            continue
                        awaiting_continuation.pop(worker_id, None)
                        active_leases.discard(job.lease_key)
                        retry_counts[job.worker_id] += 1
                        if retry_counts[job.worker_id] <= max_retries:
                            pending.append(job)
                            pending.sort(
                                key=lambda item: (
                                    estimated_tokens_by_job_id[item.worker_id],
                                    item.worker_id,
                                )
                            )
                            if metrics_tracker is not None:
                                metrics_tracker.record_retry(
                                    stage_name=stage_name,
                                    job=job,
                                    reason="failure",
                                )
                            _emit_swarm_progress(
                                progress_callback,
                                "worker_retry",
                                reason="failure",
                                failure_message=failure_message,
                                **_swarm_progress_payload(stage_name, job),
                            )
                            continue
                        if metrics_tracker is not None:
                            metrics_tracker.mark_worker_failed(
                                stage_name=stage_name,
                                job=job,
                                failure_message=failure_message or "worker continuation failed",
                            )
                        _emit_swarm_progress(
                            progress_callback,
                            "worker_failed",
                            failure_message=failure_message or "worker continuation failed",
                            **_swarm_progress_payload(stage_name, job),
                        )
                        permanent_failures.append(
                            SwarmWorkerFailureDiagnostic(
                                stage=stage_name,
                                worker_id=job.worker_id,
                                lease_key=job.lease_key,
                                failure_message=failure_message or "worker continuation failed",
                                response_id=state.pending_continuation_response_id,
                            )
                        )
                        continue
                    state.handle = handle
                    state.pending_continuation_response_id = None
                    state.pending_continuation_input = ()
                    active[worker_id] = state
                    awaiting_continuation.pop(worker_id, None)
                    scheduled_continuation = True
                    scheduled_this_pass = True
                    break
                if scheduled_continuation:
                    continue
                if hit_cooldown:
                    break

                scan_limit = len(pending)
                while pending and len(active) < configured_parallel_limit and scan_limit > 0:
                    job = pending.pop(0)
                    if job.lease_key in active_leases:
                        pending.append(job)
                        scan_limit -= 1
                        continue
                    active_jobs = tuple(state.job for state in active.values())
                    if limiter is not None:
                        if metrics_tracker is not None:
                            metrics_tracker.update_limiter_state(
                                stage_name=stage_name,
                                limiter_state=limiter.snapshot(),
                            )
                        if not limiter.can_start_job(job=job, active_jobs=active_jobs, now=now):
                            degraded_job = None
                            if limiter.is_intrinsically_oversized(job=job):
                                degraded_job = _degrade_worker_job(job)
                            if degraded_job is not None:
                                estimated_tokens_by_job_id[degraded_job.worker_id] = _estimate_job_request_tokens(
                                    degraded_job
                                )
                                pending.append(degraded_job)
                                pending.sort(
                                    key=lambda item: (
                                        estimated_tokens_by_job_id[item.worker_id],
                                        job_order_by_id[item.worker_id],
                                    )
                                )
                                if metrics_tracker is not None:
                                    metrics_tracker.record_degrade(stage_name=stage_name, job=degraded_job)
                                _emit_swarm_progress(
                                    progress_callback,
                                    "worker_degraded",
                                    **_swarm_progress_payload(stage_name, degraded_job),
                                )
                                scan_limit = len(pending)
                                continue
                            if limiter.is_intrinsically_oversized(job=job):
                                if metrics_tracker is not None:
                                    metrics_tracker.mark_worker_failed(
                                        stage_name=stage_name,
                                        job=job,
                                        failure_message="request remains larger than the safe TPM envelope after degradation",
                                    )
                                permanent_failures.append(
                                    SwarmWorkerFailureDiagnostic(
                                        stage=stage_name,
                                        worker_id=job.worker_id,
                                        lease_key=job.lease_key,
                                        failure_message="request remains larger than the safe TPM envelope after degradation",
                                    )
                                )
                                scan_limit = len(pending)
                                continue
                            pending.append(job)
                            blocked_job = blocked_job or job
                            blocked_continuation = False
                            scan_limit -= 1
                            continue
                    job_estimated_tokens = estimated_tokens_by_job_id[job.worker_id]
                    if (
                        max_inflight_estimated_tokens is not None
                        and active
                        and active_estimated_tokens + job_estimated_tokens > max_inflight_estimated_tokens
                    ):
                        pending.append(job)
                        blocked_job = blocked_job or job
                        blocked_continuation = False
                        scan_limit -= 1
                        continue
                    try:
                        handle = provider.start_background_turn(
                            model=job.model,
                            reasoning_effort=job.reasoning_effort,
                            instructions=job.instructions,
                            input_text=job.input_text,
                            previous_response_id=None,
                            tools=job.tools,
                            text_format=job.text_format,
                            prompt_cache_key=job.prompt_cache_key,
                        )
                    except Exception as exc:
                        failure_message = provider.classify_provider_failure(exc) or str(exc)
                        rate_limit_delay = _rate_limit_retry_delay_seconds(failure_message)
                        if rate_limit_delay is not None:
                            rate_limit_retry_counts[job.worker_id] += 1
                            if limiter is not None:
                                limiter.record_rate_limit(job=job, failure_message=failure_message)
                                if metrics_tracker is not None:
                                    metrics_tracker.update_limiter_state(
                                        stage_name=stage_name,
                                        limiter_state=limiter.snapshot(),
                                    )
                            retry_delay = rate_limit_delay + _rate_limit_retry_jitter_seconds(
                                worker_id=job.worker_id,
                                retry_count=rate_limit_retry_counts[job.worker_id],
                            )
                            if rate_limit_retry_counts[job.worker_id] <= rate_limit_max_retries:
                                pending.append(job)
                                pending.sort(
                                    key=lambda item: (
                                        estimated_tokens_by_job_id[item.worker_id],
                                        job_order_by_id[item.worker_id],
                                    )
                                )
                                cooldown_until = max(cooldown_until, time.monotonic() + retry_delay)
                                if metrics_tracker is not None:
                                    metrics_tracker.record_retry(
                                        stage_name=stage_name,
                                        job=job,
                                        reason="rate_limit",
                                        delay_seconds=retry_delay,
                                    )
                                _emit_swarm_progress(
                                    progress_callback,
                                    "worker_retry",
                                    reason="rate_limit",
                                    delay_seconds=retry_delay,
                                    failure_message=failure_message,
                                    **_swarm_progress_payload(stage_name, job),
                                )
                                blocked_job = job
                                blocked_continuation = False
                                hit_cooldown = True
                                break
                            diagnostic = SwarmWorkerFailureDiagnostic(
                                stage=stage_name,
                                worker_id=job.worker_id,
                                lease_key=job.lease_key,
                                failure_message=failure_message or "rate-limit retries exhausted",
                            )
                            abort_diagnostics.append(diagnostic)
                            aborting_due_to_rate_limit = True
                            if metrics_tracker is not None:
                                metrics_tracker.mark_worker_failed(
                                    stage_name=stage_name,
                                    job=job,
                                    failure_message=diagnostic.failure_message,
                                )
                                metrics_tracker.mark_stage_aborted(
                                    stage_name=stage_name,
                                    reason=diagnostic.failure_message,
                                )
                            _emit_swarm_progress(
                                progress_callback,
                                "worker_failed",
                                failure_message=diagnostic.failure_message,
                                **_swarm_progress_payload(stage_name, job),
                            )
                            scan_limit = len(pending)
                            continue
                        retry_counts[job.worker_id] += 1
                        if retry_counts[job.worker_id] <= max_retries:
                            pending.append(job)
                            pending.sort(
                                key=lambda item: (
                                    estimated_tokens_by_job_id[item.worker_id],
                                    job_order_by_id[item.worker_id],
                                )
                            )
                            if metrics_tracker is not None:
                                metrics_tracker.record_retry(
                                    stage_name=stage_name,
                                    job=job,
                                    reason="failure",
                                )
                            _emit_swarm_progress(
                                progress_callback,
                                "worker_retry",
                                reason="failure",
                                failure_message=failure_message,
                                **_swarm_progress_payload(stage_name, job),
                            )
                            scan_limit = len(pending)
                            continue
                        if metrics_tracker is not None:
                            metrics_tracker.mark_worker_failed(
                                stage_name=stage_name,
                                job=job,
                                failure_message=failure_message or "worker start failed",
                            )
                        _emit_swarm_progress(
                            progress_callback,
                            "worker_failed",
                            failure_message=failure_message or "worker start failed",
                            **_swarm_progress_payload(stage_name, job),
                        )
                        permanent_failures.append(
                            SwarmWorkerFailureDiagnostic(
                                stage=stage_name,
                                worker_id=job.worker_id,
                                lease_key=job.lease_key,
                                failure_message=failure_message or "worker start failed",
                            )
                        )
                        scan_limit = len(pending)
                        continue
                    started_at = time.monotonic()
                    active[job.worker_id] = ActiveSwarmWorker(
                        job=job,
                        handle=handle,
                        tool_traces=[],
                        started_at=started_at,
                    )
                    if metrics_tracker is not None:
                        metrics_tracker.record_attempt_started(
                            stage_name=stage_name,
                            job=job,
                            started_at_monotonic=started_at,
                        )
                    attempt_counts[job.worker_id] += 1
                    _emit_swarm_progress(
                        progress_callback,
                        "worker_started",
                        attempt=attempt_counts[job.worker_id],
                        estimated_request_tokens=job_estimated_tokens,
                        **_swarm_progress_payload(stage_name, job),
                    )
                    active_leases.add(job.lease_key)
                    scheduled_this_pass = True
                    break
                if not scheduled_this_pass:
                    break

            for worker_id, state in list(active.items()):
                def _handle_worker_event(event_type: str, data: dict[str, Any], *, job=state.job) -> None:
                    if metrics_tracker is not None:
                        metrics_tracker.record_provider_event(
                            stage_name=stage_name,
                            job=job,
                            event_type=event_type,
                            data=data,
                        )
                    if limiter is not None and event_type == "provider_usage":
                        limiter.record_provider_usage(job=job, data=data, now=time.monotonic())
                        if metrics_tracker is not None:
                            metrics_tracker.update_limiter_state(
                                stage_name=stage_name,
                                limiter_state=limiter.snapshot(),
                            )
                    if event_type != "tool_calls_requested":
                        return
                    for item in data.get("tool_calls") or []:
                        tool_name = str(item.get("name", "") or "")
                        arguments = item.get("arguments")
                        if not isinstance(arguments, dict):
                            arguments = {}
                        _emit_swarm_progress(
                            progress_callback,
                            "worker_tool_call_requested",
                            response_id=str(data.get("response_id", "") or ""),
                            tool_name=tool_name,
                            arguments=arguments,
                            summary=_tool_call_summary(
                                job=job,
                                tool_name=tool_name,
                                arguments=arguments,
                            ),
                            **_swarm_progress_payload(stage_name, job),
                        )

                try:
                    poll_result = provider.poll_background_turn(
                        handle=state.handle,
                        model=state.job.model,
                        tools=state.job.tools,
                        tool_executor=tool_executor,
                        text_format=state.job.text_format,
                        event_callback=_handle_worker_event,
                    )
                except Exception as exc:
                    poll_result = None
                    failure_message = provider.classify_provider_failure(exc) or str(exc)
                else:
                    failure_message = poll_result.failure_message
                    state.tool_traces.extend(poll_result.tool_traces)
                    _append_tool_trace_records(
                        path=tool_trace_path,
                        stage_name=stage_name,
                        job=state.job,
                        traces=poll_result.tool_traces,
                    )

                if poll_result is not None and poll_result.status == "running":
                    state.handle = ProviderBackgroundHandle(response_id=poll_result.response_id)
                    continue
                if poll_result is not None and poll_result.status == "awaiting_continuation":
                    active.pop(worker_id, None)
                    state.handle = None
                    state.pending_continuation_response_id = poll_result.response_id
                    state.pending_continuation_input = poll_result.continuation_input
                    awaiting_continuation[worker_id] = state
                    continue

                del active[worker_id]
                active_leases.discard(state.job.lease_key)
                finished_at = time.monotonic()
                if metrics_tracker is not None:
                    metrics_tracker.record_attempt_finished(
                        stage_name=stage_name,
                        job=state.job,
                        finished_at_monotonic=finished_at,
                    )

                if poll_result is not None and poll_result.status == "completed":
                    completed_result = ProviderTurnResult(
                        response_id=poll_result.response_id,
                        final_text=poll_result.final_text,
                        tool_traces=tuple(state.tool_traces),
                        status="completed",
                        model=state.job.model,
                    )
                    try:
                        if on_worker_completed is not None:
                            on_worker_completed(state.job, completed_result)
                    except Exception as exc:
                        diagnostic = SwarmWorkerFailureDiagnostic(
                            stage=stage_name,
                            worker_id=state.job.worker_id,
                            lease_key=state.job.lease_key,
                            failure_message=str(exc),
                            response_id=completed_result.response_id,
                            raw_final_text=completed_result.final_text,
                        )
                        _emit_swarm_progress(
                            progress_callback,
                            "worker_failed",
                            failure_message=diagnostic.failure_message,
                            response_id=completed_result.response_id,
                            **_swarm_progress_payload(stage_name, state.job),
                        )
                        if metrics_tracker is not None:
                            metrics_tracker.mark_worker_failed(
                                stage_name=stage_name,
                                job=state.job,
                                failure_message=diagnostic.failure_message,
                            )
                        if aborting_due_to_rate_limit:
                            abort_diagnostics.append(diagnostic)
                            continue
                        permanent_failures.append(diagnostic)
                        continue
                    if metrics_tracker is not None:
                        metrics_tracker.mark_worker_completed(stage_name=stage_name, job=state.job)
                    results[worker_id] = completed_result
                    _emit_swarm_progress(
                        progress_callback,
                        "worker_completed",
                        elapsed_seconds=round(max(0.0, finished_at - state.started_at), 6),
                        response_id=completed_result.response_id,
                        **_swarm_progress_payload(stage_name, state.job),
                    )
                    continue

                rate_limit_delay = _rate_limit_retry_delay_seconds(failure_message)
                if rate_limit_delay is not None:
                    rate_limit_retry_counts[worker_id] += 1
                    if limiter is not None:
                        limiter.record_rate_limit(job=state.job, failure_message=failure_message or "")
                        if metrics_tracker is not None:
                            metrics_tracker.update_limiter_state(
                                stage_name=stage_name,
                                limiter_state=limiter.snapshot(),
                            )
                    retry_delay = rate_limit_delay + _rate_limit_retry_jitter_seconds(
                        worker_id=worker_id,
                        retry_count=rate_limit_retry_counts[worker_id],
                    )
                    if rate_limit_retry_counts[worker_id] <= rate_limit_max_retries:
                        pending.append(state.job)
                        pending.sort(
                            key=lambda item: (
                                estimated_tokens_by_job_id[item.worker_id],
                                job_order_by_id[item.worker_id],
                            )
                        )
                        cooldown_until = max(cooldown_until, time.monotonic() + retry_delay)
                        if metrics_tracker is not None:
                            metrics_tracker.record_retry(
                                stage_name=stage_name,
                                job=state.job,
                                reason="rate_limit",
                                delay_seconds=retry_delay,
                            )
                        _emit_swarm_progress(
                            progress_callback,
                            "worker_retry",
                            reason="rate_limit",
                            delay_seconds=retry_delay,
                            failure_message=failure_message,
                            **_swarm_progress_payload(stage_name, state.job),
                        )
                        continue
                    diagnostic = SwarmWorkerFailureDiagnostic(
                        stage=stage_name,
                        worker_id=state.job.worker_id,
                        lease_key=state.job.lease_key,
                        failure_message=failure_message or "rate-limit retries exhausted",
                        response_id=(
                            poll_result.response_id if poll_result is not None else state.handle.response_id
                        ),
                        raw_final_text=poll_result.final_text if poll_result is not None else "",
                    )
                    abort_diagnostics.append(diagnostic)
                    aborting_due_to_rate_limit = True
                    if metrics_tracker is not None:
                        metrics_tracker.mark_worker_failed(
                            stage_name=stage_name,
                            job=state.job,
                            failure_message=diagnostic.failure_message,
                        )
                        metrics_tracker.mark_stage_aborted(
                            stage_name=stage_name,
                            reason=diagnostic.failure_message,
                        )
                    _emit_swarm_progress(
                        progress_callback,
                        "worker_failed",
                        failure_message=diagnostic.failure_message,
                        response_id=diagnostic.response_id,
                        **_swarm_progress_payload(stage_name, state.job),
                    )
                    continue

                retry_counts[worker_id] += 1
                if retry_counts[worker_id] <= max_retries:
                    pending.append(state.job)
                    if metrics_tracker is not None:
                        metrics_tracker.record_retry(
                            stage_name=stage_name,
                            job=state.job,
                            reason="failure",
                        )
                    _emit_swarm_progress(
                        progress_callback,
                        "worker_retry",
                        reason="failure",
                        failure_message=failure_message,
                        **_swarm_progress_payload(stage_name, state.job),
                    )
                    continue
                diagnostic = SwarmWorkerFailureDiagnostic(
                    stage=stage_name,
                    worker_id=state.job.worker_id,
                    lease_key=state.job.lease_key,
                    failure_message=failure_message or "background turn failed",
                    response_id=(
                        poll_result.response_id if poll_result is not None else state.handle.response_id
                    ),
                    raw_final_text=poll_result.final_text if poll_result is not None else "",
                )
                if metrics_tracker is not None:
                    metrics_tracker.mark_worker_failed(
                        stage_name=stage_name,
                        job=state.job,
                        failure_message=diagnostic.failure_message,
                    )
                _emit_swarm_progress(
                    progress_callback,
                    "worker_failed",
                    failure_message=diagnostic.failure_message,
                    response_id=diagnostic.response_id,
                    **_swarm_progress_payload(stage_name, state.job),
                )
                if aborting_due_to_rate_limit:
                    abort_diagnostics.append(diagnostic)
                    continue
                permanent_failures.append(diagnostic)

            if permanent_failures:
                for state in list(active.values()):
                    try:
                        if state.handle is not None:
                            provider.cancel_background_turn(state.handle)
                    except Exception:
                        pass
                    if metrics_tracker is not None:
                        metrics_tracker.record_attempt_finished(
                            stage_name=stage_name,
                            job=state.job,
                            finished_at_monotonic=time.monotonic(),
                        )
                        metrics_tracker.mark_worker_failed(
                            stage_name=stage_name,
                            job=state.job,
                            failure_message="cancelled after another worker failed",
                        )
                    _emit_swarm_progress(
                        progress_callback,
                        "worker_failed",
                        failure_message="cancelled after another worker failed",
                        response_id=state.handle.response_id if state.handle is not None else "",
                        **_swarm_progress_payload(stage_name, state.job),
                    )
                for state in list(awaiting_continuation.values()):
                    active_leases.discard(state.job.lease_key)
                    if metrics_tracker is not None:
                        metrics_tracker.mark_worker_failed(
                            stage_name=stage_name,
                            job=state.job,
                            failure_message="cancelled after another worker failed",
                        )
                    _emit_swarm_progress(
                        progress_callback,
                        "worker_failed",
                        failure_message="cancelled after another worker failed",
                        response_id=state.pending_continuation_response_id or "",
                        **_swarm_progress_payload(stage_name, state.job),
                    )
                raise SwarmWorkerFailure(permanent_failures)

            if aborting_due_to_rate_limit and not active:
                skipped_jobs = tuple(pending + [state.job for state in awaiting_continuation.values()])
                pending.clear()
                awaiting_continuation.clear()
                if metrics_tracker is not None:
                    for skipped_job in skipped_jobs:
                        metrics_tracker.mark_worker_skipped(
                            stage_name=stage_name,
                            job=skipped_job,
                            failure_message="skipped after stage abort",
                        )
                raise SwarmStageAbort(
                    stage_name=stage_name,
                    diagnostics=abort_diagnostics,
                    skipped_worker_ids=tuple(job.worker_id for job in skipped_jobs),
                )

            if pending or active or awaiting_continuation:
                sleep_for = poll_interval_seconds
                now = time.monotonic()
                if not active and cooldown_until > now:
                    sleep_for = max(sleep_for, cooldown_until - now)
                elif not active and blocked_job is not None:
                    recommended_wait = (
                        limiter.recommended_wait_seconds(
                            job=blocked_job,
                            active_jobs=(),
                            now=now,
                        )
                        if limiter is not None
                        else 0.0
                    )
                    sleep_for = max(sleep_for, recommended_wait or 0.5)
                    if metrics_tracker is not None:
                        metrics_tracker.record_wait(
                            stage_name=stage_name,
                            continuation=blocked_continuation,
                            seconds=sleep_for,
                        )
                    _emit_swarm_progress(
                        progress_callback,
                        "worker_waiting",
                        delay_seconds=round(sleep_for, 6),
                        continuation=blocked_continuation,
                        reason="safe_tpm_window",
                        **_swarm_progress_payload(stage_name, blocked_job),
                    )
                time.sleep(sleep_for)
    finally:
        if limiter is not None:
            limiter.persist()
            if metrics_tracker is not None:
                metrics_tracker.update_limiter_state(stage_name=stage_name, limiter_state=limiter.snapshot())

    _emit_swarm_progress(
        progress_callback,
        "stage_completed",
        stage_name=stage_name,
        worker_count=len(jobs),
        completed_workers=len(results),
    )
    return results


def _run_swarm_background_worker(
    *,
    provider: OpenAIResponsesProvider,
    job: SwarmWorkerJob,
    tool_executor,
    stage_name: str = "swarm",
    rate_limit_max_retries: int = DEFAULT_SWARM_MAX_RETRIES,
    cwd: Path | None = None,
    provider_name: str | None = None,
) -> ProviderTurnResult:
    return run_background_swarm_workers(
        provider=provider,
        jobs=[job],
        tool_executor=tool_executor,
        stage_name=stage_name,
        max_parallel=1,
        rate_limit_max_retries=rate_limit_max_retries,
        cwd=cwd,
        provider_name=provider_name,
    )[job.worker_id]


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
        if path.is_symlink():
            continue
        if path.is_file():
            files.append(RepoFileEntry(relative_path=value, path=path))
    return sorted(files, key=lambda entry: entry.relative_path)


def _reportable_contradiction(*, summary: str, notes: tuple[str, ...]) -> str | None:
    haystack = " ".join([summary, *notes]).lower()
    for phrase in PROOF_CONTRADICTION_PHRASES:
        if phrase in haystack:
            return phrase
    return None


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


def _proof_state_rank(proof_state: str) -> int:
    order = {
        "executed_proof": 0,
        "written_proof": 1,
        "path_grounded": 2,
        "hypothesized": 3,
    }
    return order.get(proof_state, 4)


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

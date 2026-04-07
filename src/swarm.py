from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from paths import managed_runtime_root_names
from provider_openai import (
    OpenAIResponsesProvider,
    ProviderBackgroundHandle,
    ProviderTurnResult,
    ToolTraceRecord,
)
from repo_memory import (
    RepoIdentity,
    danger_map_paths,
    migrate_legacy_repo_memory_dir,
    resolve_repo_identity,
)

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
DEFAULT_SWARM_POLL_INTERVAL_SECONDS = 0.05
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
        return self.outcome == "reportable" and self.proof_state in REPORTABLE_PROOF_STATES

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
    seed_results: tuple[SwarmSeedResult, ...]
    issue_candidates: tuple[SwarmIssueCandidate, ...]
    proof_results: tuple[SwarmProofResult, ...]
    seed_ledger: Path
    case_groups: Path
    final_ranked_findings: Path
    final_summary: Path


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


@dataclass
class ActiveSwarmWorker:
    job: SwarmWorkerJob
    handle: ProviderBackgroundHandle
    tool_traces: list[ToolTraceRecord]


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
            visible_text = text[:max_chars]
            return json.dumps(
                {
                    "path": self._display_path(path),
                    "content": _with_line_numbers(visible_text),
                    "truncated": len(text) > max_chars,
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
            reasoning_effort="high",
            instructions=prompt_asset.read_text(),
            input_text=input_text,
            prompt_cache_key=prompt_asset.prompt_cache_key,
            text_format=DANGER_MAP_RESPONSE_FORMAT,
            tools=tuple(tools.schemas()),
        ),
        tool_executor=tools.run,
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
    target_text: str,
    swarm_digest_text: str,
    shared_manifest_text: str,
) -> str:
    payload = {
        "task_type": "seed_file",
        "seed_id": seed_id,
        "lease_key": f"file:{target_file}",
        "target_file": target_file,
        "shared_manifest_markdown": shared_manifest_text,
        "swarm_digest_markdown": swarm_digest_text,
        "target_file_numbered_content": _with_line_numbers(target_text),
    }
    return json.dumps(payload, indent=2)


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


def _with_line_numbers(text: str) -> str:
    lines = text.splitlines()
    if text.endswith("\n"):
        lines.append("")
    if not lines:
        return "1 | "
    return "\n".join(f"{index:>5} | {line}" for index, line in enumerate(lines, start=1))


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
    prompt_asset = prompt_bundle.seed
    jobs: list[SwarmWorkerJob] = []
    ordered_seed_metadata: list[tuple[str, str]] = []
    for index, target_path in enumerate(eligible_files, start=1):
        seed_id = f"SEED-{index:03d}"
        target_file = display_repo_path(cwd, target_path)
        ordered_seed_metadata.append((seed_id, target_file))
        target_text = target_path.read_text(encoding="utf-8", errors="replace")
        jobs.append(
            SwarmWorkerJob(
                worker_id=seed_id,
                worker_type="seed_file",
                lease_key=f"file:{target_file}",
                model=loaded.effective.swarm.sweep_model,
                reasoning_effort="low",
                instructions=prompt_asset.read_text(),
                input_text=build_seed_input(
                    seed_id=seed_id,
                    target_file=target_file,
                    target_text=target_text,
                    swarm_digest_text=swarm_digest_text,
                    shared_manifest_text=shared_manifest_text,
                ),
                prompt_cache_key=prompt_asset.prompt_cache_key,
                text_format=SEED_RESPONSE_FORMAT,
                tools=tuple(tools.schemas()),
            )
        )

    provider_results = run_background_swarm_workers(
        provider=provider,
        jobs=jobs,
        tool_executor=tools.run,
    )
    seed_results: list[SwarmSeedResult] = []
    for seed_id, target_file in ordered_seed_metadata:
        result = provider_results[seed_id]
        payload = _parse_json_object(result.final_text)
        seed_result = parse_seed_payload(payload=payload, seed_id=seed_id, target_file=target_file)
        _write_seed_artifacts(seeds_dir, seed_result)
        seed_results.append(seed_result)

    issue_candidates = promote_issue_candidates(seed_results)
    proof_results: list[SwarmProofResult] = []
    if issue_candidates:
        proof_prompt_asset = prompt_bundle.proof
        seed_lookup = {result.seed_id: result for result in seed_results}
        proof_jobs: list[SwarmWorkerJob] = []
        for issue_candidate in issue_candidates:
            issue_seed_results = tuple(seed_lookup[seed_id] for seed_id in issue_candidate.seed_ids)
            proof_jobs.append(
                SwarmWorkerJob(
                    worker_id=issue_candidate.case_id,
                    worker_type="proof_issue",
                    lease_key=f"issue:{issue_candidate.case_id}",
                    model=loaded.effective.swarm.proof_model,
                    reasoning_effort="medium",
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
                )
            )

        proof_provider_results = run_background_swarm_workers(
            provider=provider,
            jobs=proof_jobs,
            tool_executor=tools.run,
        )
        for issue_candidate in issue_candidates:
            result = proof_provider_results[issue_candidate.case_id]
            payload = _parse_json_object(result.final_text)
            proof_result = parse_proof_payload(payload=payload, issue_candidate=issue_candidate)
            _write_proof_artifacts(proofs_dir, proof_result)
            proof_results.append(proof_result)

    seed_ledger = reports_dir / "seed_ledger.md"
    case_groups = reports_dir / "case_groups.md"
    final_ranked_findings = reports_dir / "final_ranked_findings.md"
    final_summary = reports_dir / "final_summary.md"
    write_seed_ledger(seed_ledger, seed_results)
    write_case_groups(case_groups, issue_candidates, seed_results, tuple(proof_results))
    write_final_ranked_findings(final_ranked_findings, tuple(proof_results))
    write_final_summary(
        final_summary,
        seed_results,
        issue_candidates,
        tuple(proof_results),
        seed_ledger,
        case_groups,
        final_ranked_findings,
    )

    return SwarmSweepResult(
        seeds_dir=seeds_dir,
        proofs_dir=proofs_dir,
        reports_dir=reports_dir,
        seed_results=tuple(seed_results),
        issue_candidates=issue_candidates,
        proof_results=tuple(proof_results),
        seed_ledger=seed_ledger,
        case_groups=case_groups,
        final_ranked_findings=final_ranked_findings,
        final_summary=final_summary,
    )


def run_background_swarm_workers(
    *,
    provider: OpenAIResponsesProvider,
    jobs: list[SwarmWorkerJob],
    tool_executor,
    max_parallel: int | None = None,
    poll_interval_seconds: float = DEFAULT_SWARM_POLL_INTERVAL_SECONDS,
    max_retries: int = DEFAULT_SWARM_MAX_RETRIES,
) -> dict[str, ProviderTurnResult]:
    if not jobs:
        return {}

    distinct_lease_count = len({job.lease_key for job in jobs})
    parallel_limit = max_parallel or min(DEFAULT_SWARM_MAX_PARALLEL, max(1, distinct_lease_count))

    pending: list[SwarmWorkerJob] = list(jobs)
    active: dict[str, ActiveSwarmWorker] = {}
    active_leases: set[str] = set()
    attempts: dict[str, int] = {job.worker_id: 0 for job in jobs}
    results: dict[str, ProviderTurnResult] = {}

    while pending or active:
        permanent_failures: list[str] = []
        scan_limit = len(pending)
        while pending and len(active) < parallel_limit and scan_limit > 0:
            job = pending.pop(0)
            if job.lease_key in active_leases:
                pending.append(job)
                scan_limit -= 1
                continue
            attempts[job.worker_id] += 1
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
                if attempts[job.worker_id] <= max_retries:
                    pending.append(job)
                    scan_limit = len(pending)
                    continue
                permanent_failures.append(
                    f"{job.worker_id} ({job.lease_key}): {failure_message or 'worker start failed'}"
                )
                scan_limit = len(pending)
                continue
            active[job.worker_id] = ActiveSwarmWorker(
                job=job,
                handle=handle,
                tool_traces=[],
            )
            active_leases.add(job.lease_key)
            scan_limit = len(pending)

        for worker_id, state in list(active.items()):
            try:
                poll_result = provider.poll_background_turn(
                    handle=state.handle,
                    model=state.job.model,
                    tools=state.job.tools,
                    tool_executor=tool_executor,
                    text_format=state.job.text_format,
                )
            except Exception as exc:
                poll_result = None
                failure_message = provider.classify_provider_failure(exc) or str(exc)
            else:
                failure_message = poll_result.failure_message
                state.tool_traces.extend(poll_result.tool_traces)

            if poll_result is not None and poll_result.status == "running":
                state.handle = ProviderBackgroundHandle(response_id=poll_result.response_id)
                continue

            del active[worker_id]
            active_leases.discard(state.job.lease_key)

            if poll_result is not None and poll_result.status == "completed":
                results[worker_id] = ProviderTurnResult(
                    response_id=poll_result.response_id,
                    final_text=poll_result.final_text,
                    tool_traces=tuple(state.tool_traces),
                    status="completed",
                    model=state.job.model,
                )
                continue

            if attempts[worker_id] <= max_retries:
                pending.append(state.job)
                continue
            permanent_failures.append(
                f"{state.job.worker_id} ({state.job.lease_key}): {failure_message or 'background turn failed'}"
            )

        if permanent_failures:
            for state in list(active.values()):
                try:
                    provider.cancel_background_turn(state.handle)
                except Exception:
                    pass
            raise RuntimeError("Swarm worker failure: " + "; ".join(permanent_failures))

        if pending or active:
            time.sleep(poll_interval_seconds)

    return results


def _run_swarm_background_worker(
    *,
    provider: OpenAIResponsesProvider,
    job: SwarmWorkerJob,
    tool_executor,
) -> ProviderTurnResult:
    return run_background_swarm_workers(
        provider=provider,
        jobs=[job],
        tool_executor=tool_executor,
        max_parallel=1,
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

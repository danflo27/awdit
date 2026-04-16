"""Interactive startup flow for the current awdit implementation.

Today `awdit review` resolves config-backed defaults, lets the operator review
the effective shared and slot resource lists, and writes a run-scoped snapshot
under `runs/<run_id>/resources/`. The full multi-agent audit pipeline
remains architecture-first and is still documented in the design docs.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import json
import secrets
import shutil
import sys
import textwrap
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from config import (
    SLOT_NAMES,
    ConfigError,
    apply_runtime_overrides,
    default_shared_resources_path,
    default_slot_resources_path,
    discover_resource_files,
    load_effective_config,
    merge_patch_dicts,
    render_config_scaffold,
    summarize_config,
)
from paths import migrate_legacy_runtime_layout, runs_root
from provider_openai import OpenAIResponsesProvider
from repo_memory import migrate_legacy_repo_memory_dir, resolve_repo_identity
from runtime import OneSlotRuntime
from state_db import insert_run, record_run_failure, update_run_status
from swarm import (
    append_repo_guidance,
    display_repo_path,
    freeze_swarm_prompt_bundle,
    generate_danger_map,
    list_eligible_swarm_files,
    load_danger_map_result,
    run_swarm_sweep,
    summarize_seed_request_volume,
    SwarmWorkerFailure,
)
from terminal_ui import (
    ModerateSpacingArgumentParser,
    print_line,
    print_lines,
    print_section,
    prompt_input,
)

DEFAULT_SWARM_BASE_REF = "main"


@dataclass(frozen=True)
class RuntimeResources:
    shared: tuple[str, ...]
    slots: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class RunResourceSnapshot:
    run_id: str
    run_dir: Path
    run_json: Path
    prompts_dir: Path
    shared_manifest: Path
    slot_manifests: dict[str, Path]
    summary_path: Path


@dataclass(frozen=True)
class SwarmStartupSnapshot:
    run_id: str
    run_dir: Path
    run_json: Path
    prompts_dir: Path
    prompt_bundle_manifest: Path
    shared_manifest: Path
    swarm_digest: Path


@dataclass(frozen=True)
class ResourceItemInfo:
    original: str
    resolved: str
    kind: str


def main(argv: list[str] | None = None) -> int:
    parser = ModerateSpacingArgumentParser(prog="awdit")
    subparsers = parser.add_subparsers(dest="command", parser_class=ModerateSpacingArgumentParser)

    review_parser = subparsers.add_parser(
        "review",
        help="Review config defaults, resolve run resources, and write run-scoped manifests.",
    )
    review_parser.set_defaults(handler=_handle_review)

    swarm_parser = subparsers.add_parser(
        "swarm",
        help="Run the repo-wide black-hat sweep startup flow.",
    )
    swarm_parser.add_argument(
        "--config",
        type=Path,
        help="Load swarm config from this path instead of repo-root config/config.toml.",
    )
    swarm_parser.add_argument(
        "--base-ref",
        help='Use this git base ref for `pr_changed_files` swarm mode. Defaults to "main".',
    )
    swarm_parser.set_defaults(handler=_handle_swarm)

    init_config_parser = subparsers.add_parser(
        "init-config",
        help="Write a fresh grouped config scaffold to config/config.toml.",
    )
    init_config_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite config/config.toml if it already exists.",
    )
    init_config_parser.set_defaults(handler=_handle_init_config)

    list_models_parser = subparsers.add_parser(
        "list-models",
        help="Fetch and list available models for the active provider.",
    )
    list_models_parser.set_defaults(handler=_handle_list_models)

    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.handler(args)


def _handle_review(_: argparse.Namespace) -> int:
    cwd = Path.cwd()
    migrate_legacy_runtime_layout(cwd)
    try:
        loaded = load_effective_config(cwd=cwd)
    except ConfigError as exc:
        _print_line(f"Config error: {exc}")
        return 1

    current, config_patch = _run_config_override_menu(loaded)
    _print_section_heading("Resource defaults")
    _print_note_block(
        [
            "Everything under config/resources/shared/ and config/resources/slots/<slot>/ is included automatically by default unless repo config excludes it.",
            "Use config include lists only for explicit URLs or out-of-tree defaults.",
        ]
    )

    effective_resources = _build_effective_resource_defaults(current, cwd)
    shared_resources = _review_shared_resources(effective_resources.shared, cwd)
    if shared_resources is None:
        _print_section_heading("Review canceled before launch.")
        return 0

    slot_resources = effective_resources.slots
    if _confirm("Review slot-specific resources before launch?", default=False, separated=True):
        reviewed_slots = _review_slot_resources(slot_resources, cwd)
        if reviewed_slots is None:
            _print_section_heading("Review canceled before launch.")
            return 0
        slot_resources = reviewed_slots

    final_resources = RuntimeResources(shared=shared_resources, slots=slot_resources)
    snapshot = _persist_run_resource_snapshot(cwd, current, final_resources)

    _print_section_heading("Final effective config")
    _print_summary(current)
    _print_section_heading("Run-scoped resource snapshot")
    _print_line(f"- Run id: {snapshot.run_id}")
    _print_line(f"- Run metadata: {snapshot.run_json}")
    _print_line(f"- Prompt snapshots: {snapshot.prompts_dir}")
    _print_line(f"- Shared resource manifest: {snapshot.shared_manifest}")
    for slot_name in SLOT_NAMES:
        manifest = snapshot.slot_manifests.get(slot_name)
        if manifest is None:
            continue
        label = slot_name.replace("_", " ").title()
        _print_line(f"- {label} resource manifest: {manifest}")
    _print_line(f"- Resource summary: {snapshot.summary_path}")
    _print_run_resource_summary(cwd, final_resources)

    if config_patch:
        _print_section_heading(
            f"Note: config-backed changes were not saved. Update {current.config_path} "
            "manually if you want to keep them."
        )

    if _confirm("Enter one-slot runtime prototype mode?", default=False, separated=True):
        return _run_one_slot_runtime(cwd, current, snapshot)

    _print_section_heading("Startup resource review complete.")
    _print_line("Full audit pipeline beyond startup resource staging is not implemented yet.")
    return 0


def _handle_init_config(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    config_dir = cwd / "config"
    config_path = config_dir / "config.toml"
    if config_path.exists() and not args.force:
        _print_line(
            f"Refusing to overwrite existing config at {config_path}. "
            "Re-run with --force if you want to replace it."
        )
        return 1
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(render_config_scaffold(), encoding="utf-8")
    _print_section_heading(f"Wrote grouped config scaffold: {config_path}")
    _print_line("Review the inline comments, then adjust scope, resources, prompts, and swarm overrides as needed.")
    return 0


def _handle_swarm(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    config_path = args.config.expanduser().resolve() if args.config is not None else None
    migrate_legacy_runtime_layout(cwd)
    try:
        loaded = load_effective_config(cwd=cwd, config_path=config_path)
    except ConfigError as exc:
        _print_line(f"Config error: {exc}")
        return 1

    if loaded.effective.swarm is None:
        _print_line("Config error: missing required [swarm] config block.")
        return 1
    file_mode = loaded.effective.swarm.eligible_file_profile
    if args.base_ref and file_mode != "pr_changed_files":
        _print_line('Config error: --base-ref is only valid when [swarm.files].profile = "pr_changed_files".')
        return 1
    base_ref = args.base_ref or (DEFAULT_SWARM_BASE_REF if file_mode == "pr_changed_files" else None)

    _print_section_heading("Starting new swarm run...")
    run_id, run_dir = _allocate_run_dir(cwd)
    provider = OpenAIResponsesProvider.from_loaded_config(loaded)
    identity = resolve_repo_identity(cwd)
    insert_run(
        cwd=cwd,
        run_id=run_id,
        repo_key=identity.repo_key,
        mode="swarm",
        status="starting",
        run_dir=run_dir,
    )

    current = loaded
    if _confirm("Adjust config-backed settings before swarm startup?", default=False):
        current, _ = _run_config_override_menu(loaded)

    try:
        prompt_bundle = freeze_swarm_prompt_bundle(run_dir=run_dir, loaded=current)
        result = _prepare_swarm_danger_map(
            cwd=cwd,
            loaded=current,
            provider=provider,
            prompt_bundle=prompt_bundle,
            repo_key=identity.repo_key,
        )
        effective_resources = _build_effective_resource_defaults(current, cwd)
        shared_resources = _review_swarm_shared_resources(effective_resources.shared, cwd)
        if shared_resources is None:
            update_run_status(
                cwd=cwd,
                run_id=run_id,
                status="canceled",
                completed=True,
            )
            _print_section_heading("Swarm canceled before launch.")
            return 0

        eligible_files = list_eligible_swarm_files(cwd, current, base_ref=base_ref)
        snapshot = _persist_swarm_startup_snapshot(
            cwd=cwd,
            loaded=current,
            run_id=run_id,
            run_dir=run_dir,
            shared_resources=shared_resources,
            danger_map_result=result,
            eligible_files=eligible_files,
            base_ref=base_ref,
            prompt_bundle=prompt_bundle,
        )
        _print_swarm_preflight(cwd, current, snapshot, result, prompt_bundle, eligible_files, base_ref=base_ref)

        update_run_status(
            cwd=cwd,
            run_id=run_id,
            status="preflight_ready",
            completed=False,
        )
    except Exception as exc:
        diagnostic_path = _persist_swarm_failure_diagnostic(run_id=run_id, run_dir=run_dir, exc=exc)
        update_run_status(
            cwd=cwd,
            run_id=run_id,
            status="failed",
            completed=True,
        )
        _record_swarm_failure_state(cwd=cwd, run_id=run_id, diagnostic_path=diagnostic_path, exc=exc)
        _print_section_heading(f"Swarm startup failed: {exc}")
        _print_line(f"Failure diagnostics: {diagnostic_path}")
        return 1

    if current.effective.swarm.eligible_file_profile == "pr_changed_files" and not eligible_files:
        update_run_status(
            cwd=cwd,
            run_id=run_id,
            status="completed",
            completed=True,
        )
        _print_section_heading("No processable changed files remain for swarm.")
        if base_ref is not None:
            _print_line(f"Base ref: {base_ref}")
        _print_line(
            "PR changed-files mode filtered out deleted, missing, symlinked, and runtime-managed paths."
        )
        _print_line("Swarm finished without launching workers.")
        return 0

    _print_section_heading("Swarm startup preflight is ready.")
    if not _confirm("Launch swarm?", default=True, separated=True):
        update_run_status(
            cwd=cwd,
            run_id=run_id,
            status="canceled",
            completed=True,
        )
        _print_section_heading("Swarm canceled before launch.")
        return 0

    try:
        _print_section_heading("Launching swarm batch...")
        _print_section_heading(f"Sweep stage started: {len(eligible_files)} file workers queued.")
        sweep_result = run_swarm_sweep(
            cwd=cwd,
            loaded=current,
            provider=provider,
            prompt_bundle=prompt_bundle,
            run_dir=run_dir,
            swarm_digest_path=snapshot.swarm_digest,
            shared_manifest_path=snapshot.shared_manifest,
            eligible_files=eligible_files,
            progress_callback=_print_swarm_progress,
        )
        update_run_status(
            cwd=cwd,
            run_id=run_id,
            status="completed",
            completed=True,
        )
    except Exception as exc:
        diagnostic_path = _persist_swarm_failure_diagnostic(run_id=run_id, run_dir=run_dir, exc=exc)
        update_run_status(
            cwd=cwd,
            run_id=run_id,
            status="failed",
            completed=True,
        )
        _record_swarm_failure_state(cwd=cwd, run_id=run_id, diagnostic_path=diagnostic_path, exc=exc)
        _print_section_heading(f"Swarm execution failed: {exc}")
        _print_line(f"Failure diagnostics: {diagnostic_path}")
        return 1

    _print_section_heading("Swarm complete.")
    _print_section_heading("Final artifacts")
    _print_line("  Ranked findings:")
    _print_line(f"    {sweep_result.final_ranked_findings}")
    _print_line("  Seed ledger:")
    _print_line(f"    {sweep_result.seed_ledger}")
    _print_line("  Duplicate and case groups:")
    _print_line(f"    {sweep_result.case_groups}")
    _print_line("  Proof artifacts:")
    _print_line(f"    {sweep_result.proofs_dir}")
    _print_line("  Usage summary:")
    _print_line(f"    {sweep_result.usage_summary}")
    _print_line("  Tool trace log:")
    _print_line(f"    {sweep_result.tool_trace_log}")
    _print_line("  Shared resource manifest:")
    _print_line(f"    {snapshot.shared_manifest}")
    return 0


def _persist_swarm_failure_diagnostic(*, run_id: str, run_dir: Path, exc: Exception) -> Path:
    diagnostic_path = run_dir / "swarm" / "failure_diagnostic.json"
    diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(exc, SwarmWorkerFailure):
        failures = [item.to_dict() for item in exc.diagnostics]
    else:
        failures = [
            {
                "stage": "unknown",
                "worker_id": None,
                "lease_key": None,
                "failure_message": str(exc),
                "response_id": None,
                "raw_final_text": "",
            }
        ]
    diagnostic_payload = {
        "run_id": run_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "failure_count": len(failures),
        "failures": failures,
    }
    diagnostic_path.write_text(
        json.dumps(diagnostic_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    return diagnostic_path


def _record_swarm_failure_state(
    *,
    cwd: Path,
    run_id: str,
    diagnostic_path: Path,
    exc: Exception,
) -> None:
    if isinstance(exc, SwarmWorkerFailure) and exc.primary_diagnostic is not None:
        primary = exc.primary_diagnostic
        record_run_failure(
            cwd=cwd,
            run_id=run_id,
            failure_stage=primary.stage,
            failure_worker_id=primary.worker_id,
            failure_message=primary.failure_message,
            failure_artifact=diagnostic_path,
        )
        return
    record_run_failure(
        cwd=cwd,
        run_id=run_id,
        failure_stage=None,
        failure_worker_id=None,
        failure_message=str(exc),
        failure_artifact=diagnostic_path,
    )


def _prepare_swarm_danger_map(
    *,
    cwd: Path,
    loaded,
    provider: OpenAIResponsesProvider,
    prompt_bundle,
    repo_key: str,
):
    identity = resolve_repo_identity(cwd)
    migrate_legacy_repo_memory_dir(cwd, identity)
    _print_line(f"Repository detected: `{identity.repo_name}`")

    result = load_danger_map_result(cwd, repo_key)
    if result is None:
        _print_line("No repo danger map exists for this repository yet.")
        _print_line("Swarm mode requires a repo danger map before launch.")
        result = _generate_swarm_danger_map(
            cwd=cwd,
            loaded=loaded,
            provider=provider,
            prompt_bundle=prompt_bundle,
        )
        _print_section_heading("Repo danger map ready:")
        _print_line(f"  {result.danger_map_md}")
    else:
        _print_section_heading("Existing repo danger map found:")
        _print_line(f"  {result.danger_map_md}")
        if loaded.effective.repo_memory.confirm_refresh_on_startup and _confirm(
            "Refresh repo danger map before swarm startup?",
            default=True,
        ):
            result = _generate_swarm_danger_map(
                cwd=cwd,
                loaded=loaded,
                provider=provider,
                prompt_bundle=prompt_bundle,
            )
            _print_section_heading("Updated repo danger map ready:")
            _print_line(f"  {result.danger_map_md}")

    guidance_notes: tuple[str, ...] = ()
    while True:
        _print_section_heading("Review the map, then choose:")
        _print_lines(
            [
                "  y. Accept it and continue",
                "  e. Enter corrections or guidance, then regenerate it",
                "  n. Regenerate it without extra guidance",
            ]
        )
        choice = _prompt("Accept / edit / regenerate? [Y/e/n] ", separated=True).strip().lower()
        if choice in {"", "y", "yes"}:
            return result
        if choice == "e":
            _print_section_heading("Enter corrections or guidance for danger-map regeneration:")
            guidance = _prompt("> ").strip()
            if guidance:
                append_repo_guidance(result.repo_comments_md, guidance)
                guidance_notes = (*guidance_notes, guidance)
            result = _generate_swarm_danger_map(
                cwd=cwd,
                loaded=loaded,
                provider=provider,
                prompt_bundle=prompt_bundle,
                guidance_notes=guidance_notes,
            )
            _print_section_heading("Updated repo danger map ready:")
            _print_line(f"  {result.danger_map_md}")
            continue
        if choice == "n":
            result = _generate_swarm_danger_map(
                cwd=cwd,
                loaded=loaded,
                provider=provider,
                prompt_bundle=prompt_bundle,
            )
            _print_section_heading("Updated repo danger map ready:")
            _print_line(f"  {result.danger_map_md}")
            continue
        _print_section_heading("Invalid choice. Use y, e, or n.")


def _generate_swarm_danger_map(
    *,
    cwd: Path,
    loaded,
    provider: OpenAIResponsesProvider,
    prompt_bundle,
    guidance_notes: tuple[str, ...] = (),
):
    _print_section_heading("Generating repo danger map...")
    return generate_danger_map(
        cwd=cwd,
        loaded=loaded,
        provider=provider,
        prompt_bundle=prompt_bundle,
        guidance_notes=guidance_notes,
    )


def _review_swarm_shared_resources(
    current_items: tuple[str, ...],
    cwd: Path,
) -> tuple[str, ...] | None:
    return _review_resource_list(
        current_items,
        cwd=cwd,
        title="Shared resources selected for this run",
        note_lines=[
            "Everything under config/resources/shared/ is included by default.",
            "Repo config usually only needs [resources.shared] exclude = [...].",
            "Use [resources.shared] include = [...] only for explicit URLs or out-of-tree paths.",
        ],
        prompt="Proceed / edit / exit? [Y/e/n] ",
        edit_prompt="Enter the exact shared resource list for this run, comma-separated: ",
        edit_help="You can use files from config/resources/, any other local path, folders, or URLs.",
    )


def _persist_swarm_startup_snapshot(
    *,
    cwd: Path,
    loaded,
    run_id: str,
    run_dir: Path,
    shared_resources: tuple[str, ...],
    danger_map_result,
    eligible_files: list[Path],
    base_ref: str | None,
    prompt_bundle,
) -> SwarmStartupSnapshot:
    prompts_dir = run_dir / "prompts"
    derived_context_dir = run_dir / "derived_context"
    resources_dir = run_dir / "resources"
    shared_dir = resources_dir / "shared"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    derived_context_dir.mkdir(parents=True, exist_ok=True)
    shared_dir.mkdir(parents=True, exist_ok=True)

    if loaded.effective.swarm is None:
        raise RuntimeError("Swarm config is not available.")

    _ensure_local_resources_present(shared_resources, cwd=cwd, label="shared resources")
    shared_records = _stage_resource_items(shared_resources, shared_dir / "staged")
    shared_manifest = shared_dir / "manifest.md"
    _write_resource_manifest(shared_manifest, "Shared resources", shared_records)

    swarm_digest = derived_context_dir / "swarm_digest.md"
    _write_swarm_digest(
        swarm_digest,
        loaded=loaded,
        danger_map_result=danger_map_result,
        shared_manifest=shared_manifest,
        eligible_files=eligible_files,
        base_ref=base_ref,
    )

    run_json = run_dir / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "mode": "swarm",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "config_path": str(loaded.config_path),
                "repo_key": danger_map_result.identity.repo_key,
                "danger_map_path": str(danger_map_result.danger_map_md),
                "swarm": {
                    "mode": {
                        "preset": loaded.effective.swarm.preset,
                    },
                    "models": {
                        "sweep": loaded.effective.swarm.sweep_model,
                        "proof": loaded.effective.swarm.proof_model,
                    },
                    "files": {
                        "profile": loaded.effective.swarm.eligible_file_profile,
                        "base_ref": base_ref,
                    },
                    "budget": {
                        "tokens": loaded.effective.swarm.token_budget,
                        "mode": loaded.effective.swarm.budget_mode,
                    },
                    "parallelism": {
                        "seed": loaded.effective.swarm.seed_max_parallel,
                        "proof": loaded.effective.swarm.proof_max_parallel,
                    },
                    "retries": {
                        "rate_limits": loaded.effective.swarm.rate_limit_max_retries,
                    },
                    "reasoning": {
                        "danger_map": loaded.effective.swarm.reasoning.danger_map,
                        "seed": loaded.effective.swarm.reasoning.seed,
                        "proof": loaded.effective.swarm.reasoning.proof,
                    },
                    "usage_summary": str(run_dir / "swarm" / "usage_summary.json"),
                    "tool_trace_log": str(run_dir / "swarm" / "tool_trace.jsonl"),
                    "prompt_bundle": prompt_bundle.to_dict(),
                },
                "resources": {
                    "shared": list(shared_resources),
                    "shared_manifest": str(shared_manifest),
                },
                "derived_context": {
                    "swarm_digest": str(swarm_digest),
                },
                "eligible_files": [display_repo_path(cwd, path) for path in eligible_files],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    return SwarmStartupSnapshot(
        run_id=run_id,
        run_dir=run_dir,
        run_json=run_json,
        prompts_dir=prompts_dir,
        prompt_bundle_manifest=prompt_bundle.manifest_path,
        shared_manifest=shared_manifest,
        swarm_digest=swarm_digest,
    )


def _write_swarm_digest(
    path: Path,
    *,
    loaded,
    danger_map_result,
    shared_manifest: Path,
    eligible_files: list[Path],
    base_ref: str | None,
) -> None:
    lines = [
        "# Swarm digest",
        "",
        f"- Repo key: `{danger_map_result.identity.repo_key}`",
        f"- Danger map: `{danger_map_result.danger_map_md}`",
        f"- Shared manifest: `{shared_manifest}`",
        f"- Eligible file count: `{len(eligible_files)}`",
        "",
        "## Trust boundaries",
    ]
    for item in danger_map_result.payload.get("trust_boundaries") or ["(none)"]:
        lines.append(f"- {item}")
    lines.extend(["", "## Risky sinks"])
    for item in danger_map_result.payload.get("risky_sinks") or ["(none)"]:
        lines.append(f"- {item}")
    lines.extend(["", "## Auth assumptions"])
    for item in danger_map_result.payload.get("auth_assumptions") or ["(none)"]:
        lines.append(f"- {item}")
    lines.extend(["", "## Hot paths"])
    for item in danger_map_result.payload.get("hot_paths") or ["(none)"]:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## Swarm settings",
            f"- Preset: `{loaded.effective.swarm.preset}`",
            f"- Sweep model: `{loaded.effective.swarm.sweep_model}`",
            f"- Proof model: `{loaded.effective.swarm.proof_model}`",
            f"- Danger-map reasoning: `{loaded.effective.swarm.reasoning.danger_map}`",
            f"- Seed reasoning: `{loaded.effective.swarm.reasoning.seed}`",
            f"- Proof reasoning: `{loaded.effective.swarm.reasoning.proof}`",
            f"- File handling mode: `{loaded.effective.swarm.eligible_file_profile}`",
            f"- Budget mode: `{loaded.effective.swarm.budget_mode}`",
            f"- Token budget: `{loaded.effective.swarm.token_budget}`",
        ]
    )
    if base_ref is not None:
        lines.append(f"- Base ref: `{base_ref}`")
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _render_swarm_file_mode(profile: str) -> str:
    if profile == "code_config_tests":
        return "code + config + tests"
    if profile == "pr_changed_files":
        return "PR changed files"
    return profile.replace("_", " ")


def _print_swarm_preflight(
    cwd: Path,
    loaded,
    snapshot,
    danger_map_result,
    prompt_bundle,
    eligible_files: list[Path],
    *,
    base_ref: str | None,
) -> None:
    seed_volume = summarize_seed_request_volume(
        cwd=cwd,
        loaded=loaded,
        prompt_bundle=prompt_bundle,
        run_dir=snapshot.run_dir,
        swarm_digest_path=snapshot.swarm_digest,
        shared_manifest_path=snapshot.shared_manifest,
        eligible_files=eligible_files,
    )
    _print_section_heading("Swarm preflight")
    _print_line("  Execution")
    _print_line(f"    Preset: {loaded.effective.swarm.preset}")
    _print_line(f"    Sweep model: {loaded.effective.swarm.sweep_model}")
    _print_line(f"    Proof model: {loaded.effective.swarm.proof_model}")
    _print_line(f"    Seed parallelism: {loaded.effective.swarm.seed_max_parallel}")
    _print_line(f"    Proof parallelism: {loaded.effective.swarm.proof_max_parallel}")
    _print_line(f"    Rate-limit retries: {loaded.effective.swarm.rate_limit_max_retries}")
    _print_line("")
    _print_line("  Context")
    _print_line(f"    File handling mode: {_render_swarm_file_mode(loaded.effective.swarm.eligible_file_profile)}")
    if base_ref is not None:
        _print_line(f"    Base ref: {base_ref}")
    _print_line(f"    Eligible files discovered: {len(eligible_files)}")
    _print_line("    Seed input mode: compact metadata + paged read_file")
    _print_line("    Proof stage: read-only validation")
    _print_line("    Final report style: proof-filtered findings, grouped duplicates")
    _print_line("")
    _print_line("  Safety")
    _print_line(f"    Budget mode: {loaded.effective.swarm.budget_mode}")
    _print_line(f"    Token budget: {loaded.effective.swarm.token_budget}")
    _print_line(f"    Estimated peak seed request tokens: {seed_volume.peak_parallel_estimated_tokens}")
    _print_line(
        "    Largest seed request: "
        f"{seed_volume.max_job_target_file or '(n/a)'} (~{seed_volume.max_job_estimated_tokens} tokens)"
    )
    if (
        loaded.effective.swarm.budget_mode == "advisory"
        and seed_volume.peak_parallel_estimated_tokens > loaded.effective.swarm.token_budget
    ):
        _print_line("    Warning: peak seed estimate exceeds the configured advisory budget.")
    _print_line("")
    _print_line("  Artifacts")
    _print_line(f"    Repo danger map: {danger_map_result.danger_map_md}")
    _print_line(f"    Shared resource manifest: {snapshot.shared_manifest}")
    _print_line(f"    Swarm digest: {snapshot.swarm_digest}")
    _print_line(f"    Prompt bundle manifest: {snapshot.prompt_bundle_manifest}")
    _print_line(f"    Tool trace log: {snapshot.run_dir / 'swarm' / 'tool_trace.jsonl'}")


def _print_swarm_progress(event_type: str, data: dict[str, Any]) -> None:
    stage_name = str(data.get("stage_name", "") or "").strip()
    worker_id = str(data.get("worker_id", "") or "").strip()
    label = str(data.get("label", "") or "").strip() or worker_id
    action = str(data.get("action", "") or "").strip() or f"work on {label}"

    if event_type == "stage_started":
        if stage_name == "seed":
            return
        worker_count = _coerce_progress_int(data.get("worker_count"))
        _print_section_heading(
            f"{_swarm_stage_title(stage_name)} stage started: "
            f"{worker_count} {_swarm_worker_noun(stage_name, worker_count)} queued.",
            flush=True,
        )
        return

    if event_type == "stage_completed":
        if stage_name == "seed":
            return
        completed_workers = _coerce_progress_int(data.get("completed_workers"))
        _print_section_heading(
            f"{_swarm_stage_title(stage_name)} stage complete: "
            f"{completed_workers} {_swarm_worker_noun(stage_name, completed_workers)} finished.",
            flush=True,
        )
        return

    if event_type == "worker_started":
        _print_line(f"[* {stage_name} worker {worker_id} started: {action} *]", flush=True)
        return

    if event_type == "worker_tool_call_requested":
        summary = str(data.get("summary", "") or "").strip() or f"using {data.get('tool_name', 'a tool')}"
        _print_line(
            f"[* {stage_name} worker {worker_id} is {summary} *]",
            flush=True,
        )
        return

    if event_type == "worker_waiting":
        delay_seconds = data.get("delay_seconds")
        continuation = bool(data.get("continuation"))
        wait_target = f"continuing {label}" if continuation else f"starting {label}"
        if isinstance(delay_seconds, (int, float)):
            _print_line(
                f"[* {stage_name} worker {worker_id} is waiting {float(delay_seconds):.2f}s for a safe TPM window before {wait_target} *]",
                flush=True,
            )
            return
        _print_line(
            f"[* {stage_name} worker {worker_id} is waiting for a safe TPM window before {wait_target} *]",
            flush=True,
        )
        return

    if event_type == "worker_degraded":
        _print_line(
            f"[* {stage_name} worker {worker_id} is trimming shared context before retrying {label} safely *]",
            flush=True,
        )
        return

    if event_type == "worker_retry":
        reason = str(data.get("reason", "") or "").strip()
        delay_seconds = data.get("delay_seconds")
        if reason == "rate_limit" and isinstance(delay_seconds, (int, float)):
            _print_line(
                f"[* {stage_name} worker {worker_id} retrying {label} after rate limit "
                f"({float(delay_seconds):.2f}s cooldown) *]",
                flush=True,
            )
            return
        _print_line(f"[* {stage_name} worker {worker_id} retrying {label} after {reason or 'failure'} *]", flush=True)
        return

    if event_type == "worker_completed":
        elapsed_seconds = data.get("elapsed_seconds")
        elapsed_suffix = ""
        if isinstance(elapsed_seconds, (int, float)):
            elapsed_suffix = f" ({float(elapsed_seconds):.2f}s)"
        _print_line(f"[* {stage_name} worker {worker_id} completed: {label}{elapsed_suffix} *]", flush=True)
        return

    if event_type == "worker_failed":
        failure_message = str(data.get("failure_message", "") or "").strip()
        if failure_message:
            _print_line(
                f"[* {stage_name} worker {worker_id} failed: {label} ({failure_message}) *]",
                flush=True,
            )
            return
        _print_line(f"[* {stage_name} worker {worker_id} failed: {label} *]", flush=True)


def _swarm_stage_title(stage_name: str) -> str:
    if stage_name == "seed":
        return "Sweep"
    return stage_name.replace("_", " ").title() or "Swarm"


def _swarm_worker_noun(stage_name: str, count: int) -> str:
    if stage_name == "proof":
        singular = "issue worker"
    elif stage_name == "seed":
        singular = "file worker"
    else:
        singular = "worker"
    return singular if count == 1 else f"{singular}s"


def _coerce_progress_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _handle_list_models(_: argparse.Namespace) -> int:
    cwd = Path.cwd()
    try:
        loaded = load_effective_config(cwd=cwd)
    except ConfigError as exc:
        _print_line(f"Config error: {exc}")
        return 1

    provider_name = loaded.effective.active_provider
    if provider_name != "openai":
        _print_line(f"Provider {provider_name!r} does not support live model listing yet.")
        return 1

    try:
        provider = OpenAIResponsesProvider.from_loaded_config(loaded)
        model_ids = provider.list_model_ids()
    except Exception as exc:
        _print_line(f"Failed to fetch models: {exc}")
        return 1

    _print_section_heading(f"Available {provider_name} models for this account:")
    if not model_ids:
        _print_line("- (none returned)")
        return 0
    for model_id in model_ids:
        _print_line(f"- {model_id}")
    return 0


def _run_one_slot_runtime(cwd: Path, loaded, snapshot: RunResourceSnapshot) -> int:
    transcript_path = _prototype_transcript_path(snapshot)
    with _prototype_transcript_capture(transcript_path):
        _print_section_heading("Prototype runtime setup")
        _print_line(f"Prototype transcript: {transcript_path}")
        runtime = OneSlotRuntime(
            cwd=cwd,
            loaded=loaded,
            run_dir=snapshot.run_dir,
            default_mode="foreground",
        )
        return runtime.interactive_loop()


def _run_config_override_menu(loaded):
    current = loaded
    config_patch: dict[str, object] = {}

    _print_summary(current)
    if not _confirm("Adjust config-backed settings before resource review?", default=False):
        return current, config_patch

    _print_section_heading("Override mode: change operational settings and type 'done' at any menu point to stop.")
    _print_line("Prompt files, providers, storage paths, and merge rules must be changed in config files.")

    while True:
        _print_section_heading("Override menu")
        _print_lines(
            [
                "  1. Slot models",
                "  2. Scope include globs",
                "  3. Scope exclude globs",
                "  4. Validation checks",
                "  5. Show current summary",
                "  6. Done",
            ]
        )
        choice = _prompt("> ").strip().lower()
        if choice in {"6", "done", "d"}:
            break
        if choice == "1":
            new_patch = _edit_slot_models(current)
            if new_patch:
                config_patch = merge_patch_dicts(config_patch, new_patch)
                current = apply_runtime_overrides(loaded, config_patch)
            continue
        if choice == "2":
            new_patch = _edit_scope_list(current, key="include")
            if new_patch:
                config_patch = merge_patch_dicts(config_patch, new_patch)
                current = apply_runtime_overrides(loaded, config_patch)
            continue
        if choice == "3":
            new_patch = _edit_scope_list(current, key="exclude")
            if new_patch:
                config_patch = merge_patch_dicts(config_patch, new_patch)
                current = apply_runtime_overrides(loaded, config_patch)
            continue
        if choice == "4":
            new_patch = _edit_validation_checks()
            if new_patch:
                config_patch = merge_patch_dicts(config_patch, new_patch)
                current = apply_runtime_overrides(loaded, config_patch)
            continue
        if choice == "5":
            _print_summary(current)
            continue
        _print_section_heading("Invalid choice. Pick 1-6 or type 'done'.")

    return current, config_patch


def _edit_slot_models(loaded) -> dict[str, object]:
    provider = loaded.effective.providers[loaded.effective.active_provider]
    patch: dict[str, object] = {"slots": {}}
    for slot_name in SLOT_NAMES:
        current_value = loaded.effective.slots[slot_name].default_model
        label = slot_name.replace("_", " ").title()
        _print_section_heading(f"{label} model (current: {current_value})")
        for index, model in enumerate(provider.allowed_models, start=1):
            _print_line(f"  {index}. {model}")
        raw = _prompt("Select number or press Enter to keep current: ").strip()
        if not raw:
            continue
        try:
            choice = int(raw)
        except ValueError:
            _print_section_heading("Invalid choice, keeping current.")
            continue
        if choice < 1 or choice > len(provider.allowed_models):
            _print_section_heading("Invalid choice, keeping current.")
            continue
        patch["slots"][slot_name] = {"default_model": provider.allowed_models[choice - 1]}
    if not patch["slots"]:
        return {}
    return patch


def _edit_scope_list(loaded, *, key: str) -> dict[str, object]:
    current_items = getattr(loaded.effective.scope, key)
    _print_section_heading(f"Current {key} globs: {', '.join(current_items) or '(none)'}")
    raw = _prompt(
        f"Enter comma-separated {key} globs, '-' to clear, or press Enter to keep current: "
    ).strip()
    if not raw:
        return {}
    values: list[str]
    if raw == "-":
        values = []
    else:
        values = [item.strip() for item in raw.split(",") if item.strip()]
    return {"scope": {key: values}}


def _edit_validation_checks() -> dict[str, object]:
    _print_section_heading("Replace validation checks. Leave the check name blank when you are done.")
    checks: list[dict[str, object]] = []
    while True:
        name = _prompt("Check name: ").strip()
        if not name:
            break
        command = _prompt("Command: ").strip()
        timeout_raw = _prompt("Timeout seconds: ").strip()
        if not command or not timeout_raw:
            _print_section_heading("Check skipped because command or timeout was blank.")
            continue
        try:
            timeout_seconds = int(timeout_raw)
        except ValueError:
            _print_section_heading("Timeout must be an integer. Check skipped.")
            continue
        if timeout_seconds <= 0:
            _print_section_heading("Timeout must be positive. Check skipped.")
            continue
        checks.append(
            {
                "name": name,
                "command": command,
                "timeout_seconds": timeout_seconds,
            }
        )
    if not checks:
        _print_section_heading("No validation changes captured.")
        return {}
    return {"validation": {"checks": checks}}


def _build_effective_resource_defaults(loaded, cwd: Path) -> RuntimeResources:
    shared_source = loaded.sources[("resources", "shared", "include")]
    shared_discovered = [str(path) for path in discover_resource_files(
        default_shared_resources_path(cwd),
        exclude=loaded.effective.resources.shared.exclude,
    )]
    shared_includes = _resolve_config_include_items(
        loaded.effective.resources.shared.include,
        source_base_dir=shared_source.base_dir,
        cwd=cwd,
    )

    slot_resources: dict[str, tuple[str, ...]] = {}
    for slot_name in SLOT_NAMES:
        slot_source = loaded.sources[("resources", "slots", slot_name, "include")]
        slot_discovered = [str(path) for path in discover_resource_files(
            default_slot_resources_path(slot_name, cwd),
            exclude=loaded.effective.resources.slots[slot_name].exclude,
        )]
        slot_includes = _resolve_config_include_items(
            loaded.effective.resources.slots[slot_name].include,
            source_base_dir=slot_source.base_dir,
            cwd=cwd,
        )
        slot_resources[slot_name] = tuple(slot_discovered + list(slot_includes))

    return RuntimeResources(
        shared=tuple(shared_discovered + list(shared_includes)),
        slots=slot_resources,
    )


def _resolve_config_include_items(
    items: tuple[str, ...],
    *,
    source_base_dir: Path | None,
    cwd: Path,
) -> tuple[str, ...]:
    resolved: list[str] = []
    for item in items:
        if _looks_like_url(item):
            resolved.append(item)
            continue
        path = Path(item).expanduser()
        if path.is_absolute():
            resolved.append(str(path.resolve()))
            continue
        base_dir = source_base_dir or cwd
        resolved.append(str((base_dir / path).resolve()))
    return tuple(resolved)


def _review_shared_resources(
    current_items: tuple[str, ...],
    cwd: Path,
) -> tuple[str, ...] | None:
    return _review_resource_list(
        current_items,
        cwd=cwd,
        title="Shared resources for this run",
        note_lines=[
            "Everything under config/resources/shared/ is included by default.",
            "Repo config usually only needs [resources.shared] exclude = [...].",
            "Use [resources.shared] include = [...] only for explicit URLs or out-of-tree paths.",
        ],
        prompt="Use / edit / exit? [Y/e/n] ",
        edit_prompt="Enter the exact shared resource list for this run, comma-separated: ",
        edit_help="You can use files from config/resources/, any other local path, folders, or URLs.",
    )


def _review_slot_resources(
    current_items: dict[str, tuple[str, ...]],
    cwd: Path,
) -> dict[str, tuple[str, ...]] | None:
    reviewed = {slot_name: tuple(items) for slot_name, items in current_items.items()}
    while True:
        _print_section_heading("Slot-specific resources")
        for index, slot_name in enumerate(SLOT_NAMES, start=1):
            label = slot_name.replace("_", " ").title()
            count = len(reviewed[slot_name])
            summary = f"{count} resource{'s' if count != 1 else ''}"
            _print_line(f"  {index}. {label}: {summary}")
        _print_line(f"  {len(SLOT_NAMES) + 1}. Done")
        raw = _prompt("> ").strip()
        if not raw:
            continue
        try:
            choice = int(raw)
        except ValueError:
            _print_section_heading("Invalid choice.")
            continue
        if choice == len(SLOT_NAMES) + 1:
            return reviewed
        if choice < 1 or choice > len(SLOT_NAMES):
            _print_section_heading("Invalid choice.")
            continue
        slot_name = SLOT_NAMES[choice - 1]
        label = slot_name.replace("_", " ").title()
        reviewed_items = _review_resource_list(
            reviewed[slot_name],
            cwd=cwd,
            title=f"{label} resources for this run",
            note_lines=[
                f"Everything under config/resources/slots/{slot_name}/ is included by default.",
                f"Repo config usually only needs [resources.slots.{slot_name}] exclude = [...].",
                f"Use [resources.slots.{slot_name}] include = [...] only for explicit URLs or out-of-tree paths.",
            ],
            prompt="Use / edit / exit? [Y/e/n] ",
            edit_prompt=f"Enter the exact resource list for {label}, comma-separated: ",
            edit_help="Place files anywhere convenient on disk, then point awdit at them here.",
        )
        if reviewed_items is None:
            return None
        reviewed[slot_name] = reviewed_items


def _review_resource_list(
    current_items: tuple[str, ...],
    *,
    cwd: Path,
    title: str,
    note_lines: list[str],
    prompt: str,
    edit_prompt: str,
    edit_help: str,
) -> tuple[str, ...] | None:
    while True:
        _print_resource_section(title, current_items, cwd=cwd, note_lines=note_lines)
        raw = _prompt(prompt, separated=True).strip().lower()
        if raw in {"", "y", "yes"}:
            missing = _missing_local_resources(current_items)
            if missing:
                _print_section_heading("Cannot continue with missing local resources:")
                for item in missing:
                    _print_line(f"  - `{_display_resource_item(item.resolved, cwd)}`")
                _print_line("Edit the list or exit.")
                continue
            return current_items
        if raw == "e":
            _print_section_heading(edit_help)
            edited = _prompt(edit_prompt, separated=True).strip()
            try:
                return _parse_exact_resource_list(edited, cwd)
            except ValueError as exc:
                _print_section_heading(f"Invalid resource list: {exc}")
                continue
        if raw in {"n", "no"}:
            return None
        _print_section_heading("Invalid choice. Use y, e, or n.")


def _parse_exact_resource_list(raw: str, cwd: Path) -> tuple[str, ...]:
    if not raw:
        return ()
    values = [item.strip() for item in raw.split(",") if item.strip()]
    normalized: list[str] = []
    for value in values:
        if _looks_like_url(value):
            normalized.append(value)
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (cwd / path).resolve()
        else:
            path = path.resolve()
        if not path.exists():
            raise ValueError(f"local path does not exist: {value}")
        normalized.append(str(path))
    return tuple(normalized)


def _persist_run_resource_snapshot(
    cwd: Path,
    loaded,
    resources: RuntimeResources,
) -> RunResourceSnapshot:
    migrate_legacy_runtime_layout(cwd)
    _ensure_local_resources_present(resources.shared, cwd=cwd, label="shared resources")
    for slot_name, items in resources.slots.items():
        _ensure_local_resources_present(
            items,
            cwd=cwd,
            label=f"{slot_name.replace('_', ' ')} resources",
        )
    run_id, run_dir = _allocate_run_dir(cwd)
    prompts_dir = run_dir / "prompts"
    resources_dir = run_dir / "resources"
    shared_dir = resources_dir / "shared"
    slot_root = resources_dir / "slots"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    shared_dir.mkdir(parents=True, exist_ok=True)
    slot_root.mkdir(parents=True, exist_ok=True)

    prompt_snapshot_paths: dict[str, str] = {}
    for slot_name in SLOT_NAMES:
        prompt_path = loaded.effective.slots[slot_name].prompt_file
        snapshot_path = prompts_dir / f"{slot_name}.md"
        shutil.copy2(prompt_path, snapshot_path)
        prompt_snapshot_paths[slot_name] = str(snapshot_path)

    shared_records = _stage_resource_items(resources.shared, shared_dir / "staged")
    shared_manifest = shared_dir / "manifest.md"
    _write_resource_manifest(shared_manifest, "Shared resources", shared_records)

    slot_manifests: dict[str, Path] = {}
    for slot_name in SLOT_NAMES:
        items = resources.slots[slot_name]
        if not items:
            continue
        slot_dir = slot_root / slot_name
        slot_dir.mkdir(parents=True, exist_ok=True)
        records = _stage_resource_items(items, slot_dir / "staged")
        manifest = slot_dir / "manifest.md"
        _write_resource_manifest(
            manifest,
            f"{slot_name.replace('_', ' ').title()} resources",
            records,
        )
        slot_manifests[slot_name] = manifest

    run_json = run_dir / "run.json"
    run_json.parent.mkdir(parents=True, exist_ok=True)
    run_json.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "config_path": str(loaded.config_path),
                "slots": {
                    slot_name: {
                        "model": loaded.effective.slots[slot_name].default_model,
                        "prompt_snapshot": prompt_snapshot_paths[slot_name],
                    }
                    for slot_name in SLOT_NAMES
                },
                "resources": {
                    "shared": list(resources.shared),
                    "slots": {slot_name: list(items) for slot_name, items in resources.slots.items()},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    summary_path = resources_dir / "summary.md"
    _write_resource_summary(summary_path, shared_manifest, slot_manifests)
    return RunResourceSnapshot(
        run_id=run_id,
        run_dir=run_dir,
        run_json=run_json,
        prompts_dir=prompts_dir,
        shared_manifest=shared_manifest,
        slot_manifests=slot_manifests,
        summary_path=summary_path,
    )


def _stage_resource_items(items: tuple[str, ...], staged_dir: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    staged_dir.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(items, start=1):
        resource = _classify_resource_item(item)
        if resource.kind == "url":
            records.append(
                {
                    "kind": "url",
                    "original": resource.original,
                    "resolved": resource.resolved,
                    "staged": "(not fetched)",
                }
            )
            continue

        source_path = Path(resource.original)
        if resource.kind == "missing":
            records.append(
                {
                    "kind": "missing",
                    "original": resource.original,
                    "resolved": resource.resolved,
                    "staged": "(missing)",
                }
            )
            continue

        target_name = f"{index:02d}_{source_path.name}"
        target_path = staged_dir / target_name
        if source_path.is_dir():
            shutil.copytree(source_path, target_path, dirs_exist_ok=True)
            kind = "directory"
        else:
            shutil.copy2(source_path, target_path)
            kind = "file"
        records.append(
            {
                "kind": kind,
                "original": resource.original,
                "resolved": resource.resolved,
                "staged": str(target_path),
            }
        )
    return records


def _write_resource_manifest(path: Path, title: str, records: list[dict[str, str]]) -> None:
    lines = [f"# {title}", ""]
    if not records:
        lines.append("No resources selected.")
    else:
        for index, record in enumerate(records, start=1):
            lines.extend(
                [
                    f"## {index}. {record['kind']}",
                    f"- Original: `{record['original']}`",
                    f"- Resolved: `{record['resolved']}`",
                    f"- Staged: `{record['staged']}`",
                    "",
                ]
            )
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _write_resource_summary(
    path: Path,
    shared_manifest: Path,
    slot_manifests: dict[str, Path],
) -> None:
    lines = [
        "# Resource summary",
        "",
        f"- Shared resource manifest: `{shared_manifest}`",
    ]
    if slot_manifests:
        for slot_name in SLOT_NAMES:
            manifest = slot_manifests.get(slot_name)
            if manifest is None:
                continue
            lines.append(f"- {slot_name.replace('_', ' ').title()} resource manifest: `{manifest}`")
    else:
        lines.append("- No slot-specific resource manifests were written.")
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _print_summary(loaded) -> None:
    _print_section_heading("Effective config summary")
    rows = summarize_config(loaded)
    if not rows:
        _print_line("(none)")
    else:
        label_width = max(len(label) for label, _, _ in rows)
        for label, value, source in rows:
            prefix = f"- {label.ljust(label_width)} : "
            wrapped = textwrap.wrap(
                f"{value} [{source}]",
                width=88,
                initial_indent=prefix,
                subsequent_indent=" " * len(prefix),
            )
            for line in wrapped:
                _print_line(line)
    _print_note_block(
        [
            "Resource folders under config/resources/shared/ and config/resources/slots/<slot> are included automatically by default unless repo config excludes them.",
        ]
    )


def _print_run_resource_summary(cwd: Path, resources: RuntimeResources) -> None:
    _print_section_heading("Resources selected for this run")
    _print_line("- Shared resources for this run:")
    _print_resource_items(resources.shared, cwd)
    slot_count = sum(1 for items in resources.slots.values() if items)
    _print_line(f"- Slot-specific resource sets with items: {slot_count}")


def _display_resource_item(item: str, cwd: Path) -> str:
    if _looks_like_url(item):
        return item
    path = Path(item)
    try:
        return str(path.resolve().relative_to(cwd))
    except ValueError:
        return str(path.resolve())


def _looks_like_url(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _classify_resource_item(item: str) -> ResourceItemInfo:
    if _looks_like_url(item):
        return ResourceItemInfo(original=item, resolved=item, kind="url")

    path = Path(item).expanduser()
    resolved = str(path.resolve())
    if not path.exists():
        return ResourceItemInfo(original=item, resolved=resolved, kind="missing")
    if path.is_dir():
        return ResourceItemInfo(original=item, resolved=resolved, kind="directory")
    if path.is_file():
        return ResourceItemInfo(original=item, resolved=resolved, kind="file")
    return ResourceItemInfo(original=item, resolved=resolved, kind="missing")


def _classify_resource_items(items: tuple[str, ...]) -> tuple[ResourceItemInfo, ...]:
    return tuple(_classify_resource_item(item) for item in items)


def _missing_local_resources(items: tuple[str, ...]) -> tuple[ResourceItemInfo, ...]:
    return tuple(item for item in _classify_resource_items(items) if item.kind == "missing")


def _ensure_local_resources_present(items: tuple[str, ...], *, cwd: Path, label: str) -> None:
    missing = _missing_local_resources(items)
    if not missing:
        return
    rendered = ", ".join(_display_resource_item(item.resolved, cwd) for item in missing)
    raise RuntimeError(f"Cannot stage {label}; missing local resources: {rendered}")


def _make_run_id() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S_%f") + f"_{secrets.token_hex(4)}"


def _allocate_run_dir(cwd: Path, *, max_attempts: int = 20) -> tuple[str, Path]:
    root = runs_root(cwd)
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(max_attempts):
        run_id = _make_run_id()
        run_dir = root / run_id
        try:
            run_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            continue
        return run_id, run_dir
    raise RuntimeError("Failed to allocate a unique run directory.")


def _prototype_transcript_path(snapshot: RunResourceSnapshot) -> Path:
    return snapshot.run_dir / "logs" / f"prototype__{snapshot.run_id}.txt"


class _TranscriptWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("w", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._stream.write(text)
            self._stream.flush()

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()

    def close(self) -> None:
        with self._lock:
            self._stream.close()


class _TeeTextStream:
    def __init__(self, primary, transcript_writer: _TranscriptWriter) -> None:
        self._primary = primary
        self._transcript_writer = transcript_writer

    @property
    def encoding(self) -> str | None:
        return getattr(self._primary, "encoding", None)

    def write(self, text: str) -> int:
        written = self._primary.write(text)
        self._transcript_writer.write(text)
        return written

    def flush(self) -> None:
        self._primary.flush()
        self._transcript_writer.flush()

    def isatty(self) -> bool:
        isatty = getattr(self._primary, "isatty", None)
        if callable(isatty):
            return bool(isatty())
        return False


@contextlib.contextmanager
def _prototype_transcript_capture(path: Path):
    transcript_writer = _TranscriptWriter(path)
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    original_input = builtins.input
    sys.stdout = _TeeTextStream(original_stdout, transcript_writer)
    sys.stderr = _TeeTextStream(original_stderr, transcript_writer)

    def _logged_input(prompt: str = "") -> str:
        transcript_writer.write(prompt)
        response = original_input(prompt)
        transcript_writer.write(f"{response}\n")
        return response

    builtins.input = _logged_input
    try:
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        builtins.input = original_input
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        transcript_writer.close()


def _print_line(line: str = "", *, flush: bool = False) -> None:
    print_line(line, flush=flush)


def _print_lines(lines: list[str] | tuple[str, ...], *, flush: bool = False) -> None:
    print_lines(lines, flush=flush)


def _print_section_heading(title: str, *, flush: bool = False) -> None:
    print_section(title, flush=flush)


def _prompt(prompt: str, *, separated: bool = False) -> str:
    return prompt_input(prompt, separated=separated)


def _confirm(prompt: str, *, default: bool, separated: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = _prompt(f"{prompt} {suffix} ", separated=separated).strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


def _print_note_block(lines: list[str]) -> None:
    _print_section_heading("Note for user:")
    for line in lines:
        for wrapped in textwrap.wrap(line, width=88):
            _print_line(f"  {wrapped}")


def _print_resource_items(items: tuple[str, ...], cwd: Path) -> None:
    if not items:
        _print_line("  (none)")
        return
    for index, item in enumerate(_classify_resource_items(items), start=1):
        _print_line(f"  {index}. `{_display_resource_item(item.resolved, cwd)}` [{item.kind}]")


def _print_resource_section(
    title: str,
    items: tuple[str, ...],
    *,
    cwd: Path,
    note_lines: list[str],
) -> None:
    _print_section_heading(title)
    _print_resource_items(items, cwd)
    _print_note_block(note_lines)

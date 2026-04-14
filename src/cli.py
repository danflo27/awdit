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

from config import (
    SLOT_NAMES,
    ConfigError,
    apply_runtime_overrides,
    default_shared_resources_path,
    default_slot_resources_path,
    discover_resource_files,
    load_effective_config,
    merge_patch_dicts,
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
    parser = argparse.ArgumentParser(prog="awdit")
    subparsers = parser.add_subparsers(dest="command")

    review_parser = subparsers.add_parser(
        "review",
        help="Review config defaults, resolve run resources, and write run-scoped manifests.",
    )
    review_parser.set_defaults(handler=_handle_review)

    swarm_parser = subparsers.add_parser(
        "swarm",
        help="Run the repo-wide black-hat sweep startup flow.",
    )
    swarm_parser.set_defaults(handler=_handle_swarm)

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
        print(f"Config error: {exc}")
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
        print("Review canceled before launch.")
        return 0

    slot_resources = effective_resources.slots
    if _confirm("Review slot-specific resources before launch?", default=False):
        reviewed_slots = _review_slot_resources(slot_resources, cwd)
        if reviewed_slots is None:
            print("Review canceled before launch.")
            return 0
        slot_resources = reviewed_slots

    final_resources = RuntimeResources(shared=shared_resources, slots=slot_resources)
    snapshot = _persist_run_resource_snapshot(cwd, current, final_resources)

    print("")
    print("Final effective config")
    _print_summary(current)
    _print_run_resource_summary(cwd, final_resources)
    print("")
    print("Run-scoped resource snapshot")
    print(f"- Run id: {snapshot.run_id}")
    print(f"- Run metadata: {snapshot.run_json}")
    print(f"- Prompt snapshots: {snapshot.prompts_dir}")
    print(f"- Shared resource manifest: {snapshot.shared_manifest}")
    for slot_name in SLOT_NAMES:
        manifest = snapshot.slot_manifests.get(slot_name)
        if manifest is None:
            continue
        label = slot_name.replace("_", " ").title()
        print(f"- {label} resource manifest: {manifest}")
    print(f"- Resource summary: {snapshot.summary_path}")

    if config_patch:
        print("")
        print(
            f"Note: config-backed changes were not saved. Update {current.config_path} "
            "manually if you want to keep them."
        )

    print("")
    if _confirm("Enter one-slot runtime prototype mode?", default=False):
        return _run_one_slot_runtime(cwd, current, snapshot)

    print("")
    print("Startup resource review complete.")
    print("Full audit pipeline beyond startup resource staging is not implemented yet.")
    return 0


def _handle_swarm(_: argparse.Namespace) -> int:
    cwd = Path.cwd()
    migrate_legacy_runtime_layout(cwd)
    try:
        loaded = load_effective_config(cwd=cwd)
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 1

    if loaded.effective.swarm is None:
        print("Config error: missing required [swarm] config block.")
        return 1

    print("Starting new swarm run...")
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
            print("Swarm canceled before launch.")
            return 0

        eligible_files = list_eligible_swarm_files(cwd, current)
        snapshot = _persist_swarm_startup_snapshot(
            cwd=cwd,
            loaded=current,
            run_id=run_id,
            run_dir=run_dir,
            shared_resources=shared_resources,
            danger_map_result=result,
            eligible_files=eligible_files,
            prompt_bundle=prompt_bundle,
        )
        _print_swarm_preflight(cwd, current, snapshot, result, prompt_bundle, eligible_files)

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
        print(f"Swarm startup failed: {exc}")
        print(f"Failure diagnostics: {diagnostic_path}")
        return 1

    print("")
    print("Swarm startup preflight is ready.")
    if not _confirm("Launch swarm?", default=True):
        update_run_status(
            cwd=cwd,
            run_id=run_id,
            status="canceled",
            completed=True,
        )
        print("Swarm canceled before launch.")
        return 0

    try:
        print("Launching swarm batch...")
        print(f"Sweep stage started: {len(eligible_files)} file workers queued.")
        sweep_result = run_swarm_sweep(
            cwd=cwd,
            loaded=current,
            provider=provider,
            prompt_bundle=prompt_bundle,
            run_dir=run_dir,
            swarm_digest_path=snapshot.swarm_digest,
            shared_manifest_path=snapshot.shared_manifest,
            eligible_files=eligible_files,
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
        print(f"Swarm execution failed: {exc}")
        print(f"Failure diagnostics: {diagnostic_path}")
        return 1

    print("")
    print("Swarm complete.")
    print("")
    print("Final artifacts")
    print("  Ranked findings:")
    print(f"    {sweep_result.final_ranked_findings}")
    print("  Seed ledger:")
    print(f"    {sweep_result.seed_ledger}")
    print("  Duplicate and case groups:")
    print(f"    {sweep_result.case_groups}")
    print("  Proof artifacts:")
    print(f"    {sweep_result.proofs_dir}")
    print("  Shared resource manifest:")
    print(f"    {snapshot.shared_manifest}")
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
    print(f"Repository detected: `{identity.repo_name}`")

    result = load_danger_map_result(cwd, repo_key)
    if result is None:
        print("No repo danger map exists for this repository yet.")
        print("Swarm mode requires a repo danger map before launch.")
        result = _generate_swarm_danger_map(
            cwd=cwd,
            loaded=loaded,
            provider=provider,
            prompt_bundle=prompt_bundle,
        )
        print("")
        print("Repo danger map ready:")
        print(f"  {result.danger_map_md}")
    else:
        print("Existing repo danger map found:")
        print(f"  {result.danger_map_md}")
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
            print("")
            print("Updated repo danger map ready:")
            print(f"  {result.danger_map_md}")

    guidance_notes: tuple[str, ...] = ()
    while True:
        print("Review the map, then choose:")
        print("  y. Accept it and continue")
        print("  e. Enter corrections or guidance, then regenerate it")
        print("  n. Regenerate it without extra guidance")
        choice = input("Accept / edit / regenerate? [Y/e/n] ").strip().lower()
        if choice in {"", "y", "yes"}:
            return result
        if choice == "e":
            print("")
            print("Enter corrections or guidance for danger-map regeneration:")
            guidance = input("> ").strip()
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
            print("")
            print("Updated repo danger map ready:")
            print(f"  {result.danger_map_md}")
            continue
        if choice == "n":
            result = _generate_swarm_danger_map(
                cwd=cwd,
                loaded=loaded,
                provider=provider,
                prompt_bundle=prompt_bundle,
            )
            print("")
            print("Updated repo danger map ready:")
            print(f"  {result.danger_map_md}")
            continue
        print("Invalid choice. Use y, e, or n.")


def _generate_swarm_danger_map(
    *,
    cwd: Path,
    loaded,
    provider: OpenAIResponsesProvider,
    prompt_bundle,
    guidance_notes: tuple[str, ...] = (),
):
    print("Generating repo danger map...")
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
                    "sweep_model": loaded.effective.swarm.sweep_model,
                    "proof_model": loaded.effective.swarm.proof_model,
                    "reasoning": {
                        "danger_map": loaded.effective.swarm.reasoning.danger_map,
                        "seed": loaded.effective.swarm.reasoning.seed,
                        "proof": loaded.effective.swarm.reasoning.proof,
                    },
                    "eligible_file_profile": loaded.effective.swarm.eligible_file_profile,
                    "token_budget": loaded.effective.swarm.token_budget,
                    "allow_no_limit": loaded.effective.swarm.allow_no_limit,
                    "seed_max_parallel": loaded.effective.swarm.seed_max_parallel,
                    "proof_max_parallel": loaded.effective.swarm.proof_max_parallel,
                    "rate_limit_max_retries": loaded.effective.swarm.rate_limit_max_retries,
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
            f"- Sweep model: `{loaded.effective.swarm.sweep_model}`",
            f"- Proof model: `{loaded.effective.swarm.proof_model}`",
            f"- Danger-map reasoning: `{loaded.effective.swarm.reasoning.danger_map}`",
            f"- Seed reasoning: `{loaded.effective.swarm.reasoning.seed}`",
            f"- Proof reasoning: `{loaded.effective.swarm.reasoning.proof}`",
            f"- Eligible profile: `{loaded.effective.swarm.eligible_file_profile}`",
        ]
    )
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _print_swarm_preflight(cwd: Path, loaded, snapshot, danger_map_result, prompt_bundle, eligible_files: list[Path]) -> None:
    seed_volume = summarize_seed_request_volume(
        cwd=cwd,
        loaded=loaded,
        prompt_bundle=prompt_bundle,
        run_dir=snapshot.run_dir,
        swarm_digest_path=snapshot.swarm_digest,
        shared_manifest_path=snapshot.shared_manifest,
        eligible_files=eligible_files,
    )
    print("")
    print("Swarm preflight")
    print("  Mode: repo-wide black-hat sweep")
    print(
        "  File profile: "
        f"{loaded.effective.swarm.eligible_file_profile.replace('_', ' ')}"
    )
    print(f"  Eligible files discovered: {len(eligible_files)}")
    if loaded.effective.swarm.allow_no_limit:
        print(
            "  Token budget: "
            f"{loaded.effective.swarm.token_budget} (advisory, no-limit allowed)"
        )
    else:
        print(f"  Token budget: {loaded.effective.swarm.token_budget}")
    print(f"  Sweep model: {loaded.effective.swarm.sweep_model}")
    print(f"  Proof model: {loaded.effective.swarm.proof_model}")
    print(f"  Seed max parallel: {loaded.effective.swarm.seed_max_parallel}")
    print(f"  Proof max parallel: {loaded.effective.swarm.proof_max_parallel}")
    print(f"  Rate-limit retries: {loaded.effective.swarm.rate_limit_max_retries}")
    print(f"  Danger-map reasoning: {loaded.effective.swarm.reasoning.danger_map}")
    print(f"  Seed reasoning: {loaded.effective.swarm.reasoning.seed}")
    print(f"  Proof reasoning: {loaded.effective.swarm.reasoning.proof}")
    print(f"  Estimated peak seed request tokens: {seed_volume.peak_parallel_estimated_tokens}")
    if (
        loaded.effective.swarm.allow_no_limit
        and seed_volume.peak_parallel_estimated_tokens > loaded.effective.swarm.token_budget
    ):
        print(
            "  Warning: estimated peak seed request tokens exceed the configured token budget, "
            "but allow_no_limit is enabled."
        )
        print(
            "    Largest seed request: "
            f"{seed_volume.max_job_target_file} (~{seed_volume.max_job_estimated_tokens} tokens)"
        )
    print("  Proof stage: read-only validation")
    print("  Final report style: proof-filtered findings, grouped duplicates")
    print("  Repo danger map:")
    print(f"    {danger_map_result.danger_map_md}")
    print("  Shared resource manifest:")
    print(f"    {snapshot.shared_manifest}")
    print("  Swarm digest:")
    print(f"    {snapshot.swarm_digest}")
    print("  Prompt bundle manifest:")
    print(f"    {snapshot.prompt_bundle_manifest}")


def _handle_list_models(_: argparse.Namespace) -> int:
    cwd = Path.cwd()
    try:
        loaded = load_effective_config(cwd=cwd)
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 1

    provider_name = loaded.effective.active_provider
    if provider_name != "openai":
        print(f"Provider {provider_name!r} does not support live model listing yet.")
        return 1

    try:
        provider = OpenAIResponsesProvider.from_loaded_config(loaded)
        model_ids = provider.list_model_ids()
    except Exception as exc:
        print(f"Failed to fetch models: {exc}")
        return 1

    print(f"Available {provider_name} models for this account:")
    if not model_ids:
        print("- (none returned)")
        return 0
    for model_id in model_ids:
        print(f"- {model_id}")
    return 0


def _run_one_slot_runtime(cwd: Path, loaded, snapshot: RunResourceSnapshot) -> int:
    transcript_path = _prototype_transcript_path(snapshot)
    with _prototype_transcript_capture(transcript_path):
        print("Prototype runtime setup")
        print(f"Prototype transcript: {transcript_path}")
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

    print("")
    print("Override mode: change operational settings and type 'done' at any menu point to stop.")
    print("Prompt files, providers, storage paths, and merge rules must be changed in config files.")

    while True:
        print("")
        print("Override menu")
        print("  1. Slot models")
        print("  2. Scope include globs")
        print("  3. Scope exclude globs")
        print("  4. Validation checks")
        print("  5. Show current summary")
        print("  6. Done")
        choice = input("> ").strip().lower()
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
        print("Invalid choice. Pick 1-6 or type 'done'.")

    return current, config_patch


def _edit_slot_models(loaded) -> dict[str, object]:
    provider = loaded.effective.providers[loaded.effective.active_provider]
    patch: dict[str, object] = {"slots": {}}
    for slot_name in SLOT_NAMES:
        current_value = loaded.effective.slots[slot_name].default_model
        label = slot_name.replace("_", " ").title()
        print("")
        print(f"{label} model (current: {current_value})")
        for index, model in enumerate(provider.allowed_models, start=1):
            print(f"  {index}. {model}")
        raw = input("Select number or press Enter to keep current: ").strip()
        if not raw:
            continue
        try:
            choice = int(raw)
        except ValueError:
            print("Invalid choice, keeping current.")
            continue
        if choice < 1 or choice > len(provider.allowed_models):
            print("Invalid choice, keeping current.")
            continue
        patch["slots"][slot_name] = {"default_model": provider.allowed_models[choice - 1]}
    if not patch["slots"]:
        return {}
    return patch


def _edit_scope_list(loaded, *, key: str) -> dict[str, object]:
    current_items = getattr(loaded.effective.scope, key)
    print("")
    print(f"Current {key} globs: {', '.join(current_items) or '(none)'}")
    raw = input(
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
    print("")
    print("Replace validation checks. Leave the check name blank when you are done.")
    checks: list[dict[str, object]] = []
    while True:
        name = input("Check name: ").strip()
        if not name:
            break
        command = input("Command: ").strip()
        timeout_raw = input("Timeout seconds: ").strip()
        if not command or not timeout_raw:
            print("Check skipped because command or timeout was blank.")
            continue
        try:
            timeout_seconds = int(timeout_raw)
        except ValueError:
            print("Timeout must be an integer. Check skipped.")
            continue
        if timeout_seconds <= 0:
            print("Timeout must be positive. Check skipped.")
            continue
        checks.append(
            {
                "name": name,
                "command": command,
                "timeout_seconds": timeout_seconds,
            }
        )
    if not checks:
        print("No validation changes captured.")
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
        print("")
        print("Slot-specific resources")
        for index, slot_name in enumerate(SLOT_NAMES, start=1):
            label = slot_name.replace("_", " ").title()
            count = len(reviewed[slot_name])
            summary = f"{count} resource{'s' if count != 1 else ''}"
            print(f"  {index}. {label}: {summary}")
        print(f"  {len(SLOT_NAMES) + 1}. Done")
        raw = input("> ").strip()
        if not raw:
            continue
        try:
            choice = int(raw)
        except ValueError:
            print("Invalid choice.")
            continue
        if choice == len(SLOT_NAMES) + 1:
            return reviewed
        if choice < 1 or choice > len(SLOT_NAMES):
            print("Invalid choice.")
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
        raw = input(prompt).strip().lower()
        if raw in {"", "y", "yes"}:
            missing = _missing_local_resources(current_items)
            if missing:
                print("")
                print("Cannot continue with missing local resources:")
                for item in missing:
                    print(f"  - `{_display_resource_item(item.resolved, cwd)}`")
                print("Edit the list or exit.")
                continue
            return current_items
        if raw == "e":
            print("")
            print(edit_help)
            edited = input(edit_prompt).strip()
            try:
                return _parse_exact_resource_list(edited, cwd)
            except ValueError as exc:
                print(f"Invalid resource list: {exc}")
                continue
        if raw in {"n", "no"}:
            return None
        print("Invalid choice. Use y, e, or n.")


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
        print("(none)")
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
                print(line)
    _print_note_block(
        [
            "Resource folders under config/resources/shared/ and config/resources/slots/<slot> are included automatically by default unless repo config excludes them.",
        ]
    )


def _print_run_resource_summary(cwd: Path, resources: RuntimeResources) -> None:
    print("- Shared resources for this run:")
    _print_resource_items(resources.shared, cwd)
    slot_count = sum(1 for items in resources.slots.values() if items)
    print(f"- Slot-specific resource sets with items: {slot_count}")


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


def _confirm(prompt: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{prompt} {suffix} ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


def _print_section_heading(title: str) -> None:
    print("")
    print(title)


def _print_note_block(lines: list[str]) -> None:
    print("Note for user:")
    for line in lines:
        for wrapped in textwrap.wrap(line, width=88):
            print(f"  {wrapped}")


def _print_resource_items(items: tuple[str, ...], cwd: Path) -> None:
    if not items:
        print("  (none)")
        return
    for index, item in enumerate(_classify_resource_items(items), start=1):
        print(f"  {index}. `{_display_resource_item(item.resolved, cwd)}` [{item.kind}]")


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

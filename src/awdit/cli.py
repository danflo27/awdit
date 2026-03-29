"""Interactive startup flow for the current awdit implementation.

Today `awdit review` resolves config-backed defaults, lets the operator review
the effective shared and slot resource lists, and writes a run-scoped snapshot
under `.awdit/runs/<run_id>/resources/`. The full multi-agent audit pipeline
remains architecture-first and is still documented in the design docs.
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from awdit.config import (
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


@dataclass(frozen=True)
class RuntimeResources:
    shared: tuple[str, ...]
    slots: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class RunResourceSnapshot:
    run_id: str
    run_dir: Path
    run_json: Path
    shared_manifest: Path
    slot_manifests: dict[str, Path]
    summary_path: Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="awdit")
    subparsers = parser.add_subparsers(dest="command")

    review_parser = subparsers.add_parser(
        "review",
        help="Review config defaults, resolve run resources, and write run-scoped manifests.",
    )
    review_parser.set_defaults(handler=_handle_review)

    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.handler(args)


def _handle_review(_: argparse.Namespace) -> int:
    cwd = Path.cwd()
    try:
        loaded = load_effective_config(cwd=cwd)
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 1

    current, config_patch = _run_config_override_menu(loaded)
    print("")
    print("Resource defaults note:")
    print("  Everything under config/resources/shared/ and config/resources/slots/<slot>/")
    print("  is included automatically by default unless repo config excludes it.")
    print("  Use config include lists only for explicit URLs or out-of-tree defaults.")

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
            f"Note: config-backed changes were not saved. Update {current.repo_config_path} "
            "manually if you want to keep them."
        )

    print("")
    print("Startup resource review complete.")
    print("Full audit pipeline beyond startup resource staging is not implemented yet.")
    return 0


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
    print("")
    print("Shared resources available for this run:")
    print("  Everything under config/resources/shared/ is included by default.")
    print("  Repo config usually only needs [resources.shared] exclude = [...].")
    print("  Use [resources.shared] include = [...] only for explicit URLs or out-of-tree paths.")
    return _review_resource_list(
        current_items,
        cwd=cwd,
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
        print("")
        print(f"{label} resources available for this run:")
        print(f"  Everything under config/resources/slots/{slot_name}/ is included by default.")
        print(f"  Repo config usually only needs [resources.slots.{slot_name}] exclude = [...].")
        print(
            f"  Use [resources.slots.{slot_name}] include = [...] only for explicit URLs or out-of-tree paths."
        )
        reviewed_items = _review_resource_list(
            reviewed[slot_name],
            cwd=cwd,
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
    prompt: str,
    edit_prompt: str,
    edit_help: str,
) -> tuple[str, ...] | None:
    while True:
        _print_resource_list(current_items, cwd)
        raw = input(prompt).strip().lower()
        if raw in {"", "y", "yes"}:
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


def _print_resource_list(items: tuple[str, ...], cwd: Path) -> None:
    if not items:
        print("  (none)")
        return
    for index, item in enumerate(items, start=1):
        print(f"  {index}. `{_display_resource_item(item, cwd)}`")


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
    run_id = _make_run_id()
    run_dir = cwd / ".awdit" / "runs" / run_id
    resources_dir = run_dir / "resources"
    shared_dir = resources_dir / "shared"
    slot_root = resources_dir / "slots"
    shared_dir.mkdir(parents=True, exist_ok=True)
    slot_root.mkdir(parents=True, exist_ok=True)

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
                "repo_config_path": str(loaded.repo_config_path),
                "user_config_path": str(loaded.user_config_path),
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
        shared_manifest=shared_manifest,
        slot_manifests=slot_manifests,
        summary_path=summary_path,
    )


def _stage_resource_items(items: tuple[str, ...], staged_dir: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    staged_dir.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(items, start=1):
        if _looks_like_url(item):
            records.append(
                {
                    "kind": "url",
                    "original": item,
                    "resolved": item,
                    "staged": "(not fetched)",
                }
            )
            continue

        source_path = Path(item)
        if not source_path.exists():
            records.append(
                {
                    "kind": "missing",
                    "original": item,
                    "resolved": item,
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
                "original": item,
                "resolved": str(source_path.resolve()),
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
    print("")
    print("Effective config summary")
    print("- Resource folders under config/resources/shared/ and config/resources/slots/<slot>")
    print("  are included automatically by default unless excluded in repo config.")
    for label, value, source in summarize_config(loaded):
        print(f"- {label}: {value} [{source}]")


def _print_run_resource_summary(cwd: Path, resources: RuntimeResources) -> None:
    print("- Shared resources for this run:")
    if resources.shared:
        for item in resources.shared:
            print(f"  {_display_resource_item(item, cwd)}")
    else:
        print("  (none)")
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


def _make_run_id() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def _confirm(prompt: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{prompt} {suffix} ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}

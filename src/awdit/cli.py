from __future__ import annotations

import argparse

from awdit.config import (
    SLOT_NAMES,
    ConfigError,
    apply_runtime_overrides,
    load_effective_config,
    merge_patch_dicts,
    summarize_config,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="awdit")
    subparsers = parser.add_subparsers(dest="command")

    review_parser = subparsers.add_parser("review", help="Review and confirm the effective config.")
    review_parser.set_defaults(handler=_handle_review)

    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.handler(args)


def _handle_review(_: argparse.Namespace) -> int:
    try:
        loaded = load_effective_config()
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 1

    runtime_attachments: dict[str, list[str]] = {}
    config_patch: dict[str, object] = {}
    current = loaded

    _print_summary(current, runtime_attachments)
    if _confirm("Use these defaults?", default=True):
        print("Config review complete. Audit pipeline is not implemented yet.")
        return 0

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
        print("  5. External resources (runtime only)")
        print("  6. Show current summary")
        print("  7. Done")
        choice = input("> ").strip().lower()
        if choice in {"7", "done", "d"}:
            break
        if choice == "1":
            new_patch = _edit_slot_models(current)
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
            runtime_attachments = _edit_runtime_attachments(runtime_attachments)
            continue
        if choice == "6":
            _print_summary(current, runtime_attachments)
            continue
        print("Invalid choice. Pick 1-7 or type 'done'.")

    print("")
    print("Final effective config")
    _print_summary(current, runtime_attachments)

    if config_patch:
        print(
            f"Note: config-backed changes were not saved. Update {current.repo_config_path} "
            "manually if you want to keep them."
        )
    if runtime_attachments:
        print("Note: external resources are runtime-only and are not saved to config.")

    print("Config review complete. Audit pipeline is not implemented yet.")
    return 0


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


def _edit_runtime_attachments(current: dict[str, list[str]]) -> dict[str, list[str]]:
    attachments = {slot: list(paths) for slot, paths in current.items()}
    while True:
        print("")
        print("External resources (runtime only)")
        for index, slot_name in enumerate(SLOT_NAMES, start=1):
            current_value = ", ".join(attachments.get(slot_name, [])) or "(none)"
            print(f"  {index}. {slot_name.replace('_', ' ').title()}: {current_value}")
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
            return attachments
        if choice < 1 or choice > len(SLOT_NAMES):
            print("Invalid choice.")
            continue
        slot_name = SLOT_NAMES[choice - 1]
        raw_paths = input(
            "Enter comma-separated local file paths, '-' to clear, or press Enter to keep current: "
        ).strip()
        if not raw_paths:
            continue
        if raw_paths == "-":
            attachments.pop(slot_name, None)
            continue
        attachments[slot_name] = [item.strip() for item in raw_paths.split(",") if item.strip()]


def _print_summary(loaded, runtime_attachments: dict[str, list[str]]) -> None:
    print("")
    print("Effective config summary")
    for label, value, source in summarize_config(loaded):
        print(f"- {label}: {value} [{source}]")
    if runtime_attachments:
        print("- Runtime attachments:")
        for slot_name in SLOT_NAMES:
            if slot_name not in runtime_attachments:
                continue
            label = slot_name.replace("_", " ").title()
            value = ", ".join(runtime_attachments[slot_name])
            print(f"  {label}: {value} [runtime override]")


def _confirm(prompt: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{prompt} {suffix} ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}

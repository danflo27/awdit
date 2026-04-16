from __future__ import annotations

import copy
import json
import os
import textwrap
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
import tomllib


SLOT_NAMES = (
    "hunter_1",
    "hunter_2",
    "skeptic_1",
    "skeptic_2",
    "referee_1",
    "referee_2",
    "solver_1",
    "solver_2",
)
REASONING_EFFORT_VALUES = ("low", "medium", "high")
SWARM_ELIGIBLE_FILE_PROFILES = ("code_config_tests", "pr_changed_files")
SWARM_PRESET_VALUES = ("safe", "balanced", "fast")
SWARM_BUDGET_MODE_VALUES = ("advisory", "enforced")
DEFAULT_SWARM_REASONING_EFFORTS = {
    "danger_map": "high",
    "seed": "low",
    "proof": "medium",
}
DEFAULT_SWARM_SEED_MAX_PARALLEL = 2
DEFAULT_SWARM_PROOF_MAX_PARALLEL = 1
DEFAULT_SWARM_RATE_LIMIT_MAX_RETRIES = 3
DEFAULT_SWARM_TOKEN_BUDGET = 120000
DEFAULT_SWARM_PRESET = "safe"
DEFAULT_SWARM_BUDGET_MODE = "enforced"
SWARM_LEGACY_KEYS = {
    "sweep_model",
    "proof_model",
    "eligible_file_profile",
    "token_budget",
    "allow_no_limit",
    "seed_max_parallel",
    "proof_max_parallel",
    "rate_limit_max_retries",
}
SWARM_PRESET_DEFAULTS: dict[str, dict[str, Any]] = {
    "safe": {
        "sweep_model": "gpt-5.4-mini",
        "proof_model": "gpt-5.4-mini",
        "eligible_file_profile": "code_config_tests",
        "token_budget": DEFAULT_SWARM_TOKEN_BUDGET,
        "budget_mode": "enforced",
        "seed_max_parallel": 2,
        "proof_max_parallel": 1,
        "rate_limit_max_retries": DEFAULT_SWARM_RATE_LIMIT_MAX_RETRIES,
    },
    "balanced": {
        "sweep_model": "gpt-5.4-mini",
        "proof_model": "gpt-5.4-mini",
        "eligible_file_profile": "code_config_tests",
        "token_budget": DEFAULT_SWARM_TOKEN_BUDGET,
        "budget_mode": "enforced",
        "seed_max_parallel": 3,
        "proof_max_parallel": 1,
        "rate_limit_max_retries": DEFAULT_SWARM_RATE_LIMIT_MAX_RETRIES,
    },
    "fast": {
        "sweep_model": "gpt-5.4-mini",
        "proof_model": "gpt-5.4-mini",
        "eligible_file_profile": "code_config_tests",
        "token_budget": DEFAULT_SWARM_TOKEN_BUDGET,
        "budget_mode": "advisory",
        "seed_max_parallel": 4,
        "proof_max_parallel": 2,
        "rate_limit_max_retries": DEFAULT_SWARM_RATE_LIMIT_MAX_RETRIES,
    },
}

BUILTIN_DEFAULTS: dict[str, Any] = {
    "active_provider": "openai",
    "providers": {
        "openai": {
            "base_url": "https://api.openai.com/v1",
        }
    },
    "scope": {
        "include": [],
        "exclude": [],
    },
    "validation": {
        "checks": [],
    },
    "repo_memory": {
        "enabled": True,
        "require_danger_map_approval": True,
        "confirm_refresh_on_startup": True,
        "auto_update_on_completion": True,
    },
    "resources": {
        "shared": {
            "include": [],
            "exclude": [],
        },
        "slots": {
            slot_name: {
                "include": [],
                "exclude": [],
            }
            for slot_name in SLOT_NAMES
        },
    },
    "github": {
        "prefer_gh": True,
    },
    "swarm": {},
}

CONFIG_BACKED_RUNTIME_KEYS = {"slots", "scope", "validation", "repo_memory", "resources"}
PathKey = tuple[str, ...]


class ConfigError(RuntimeError):
    """Raised when awdit config is missing or invalid."""


@dataclass(frozen=True)
class SourceInfo:
    label: str
    base_dir: Path | None


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    command: str
    timeout_seconds: int


@dataclass(frozen=True)
class SlotConfig:
    default_model: str
    reasoning_effort: str | None
    prompt_file: Path


@dataclass(frozen=True)
class ProviderConfig:
    api_key_env: str
    base_url: str
    allowed_models: tuple[str, ...]


@dataclass(frozen=True)
class ScopeConfig:
    include: tuple[str, ...]
    exclude: tuple[str, ...]


@dataclass(frozen=True)
class GithubConfig:
    prefer_gh: bool


@dataclass(frozen=True)
class RepoMemoryConfig:
    enabled: bool
    require_danger_map_approval: bool
    confirm_refresh_on_startup: bool
    auto_update_on_completion: bool


@dataclass(frozen=True)
class ResourceSectionConfig:
    include: tuple[str, ...]
    exclude: tuple[str, ...]


@dataclass(frozen=True)
class ResourcesConfig:
    shared: ResourceSectionConfig
    slots: dict[str, ResourceSectionConfig]


@dataclass(frozen=True)
class SwarmPromptsConfig:
    danger_map: Path
    seed: Path
    proof: Path


@dataclass(frozen=True)
class SwarmReasoningConfig:
    danger_map: str
    seed: str
    proof: str


@dataclass(frozen=True)
class SwarmConfig:
    prompts: SwarmPromptsConfig
    reasoning: SwarmReasoningConfig
    preset: str
    sweep_model: str
    proof_model: str
    eligible_file_profile: str
    token_budget: int
    budget_mode: str
    seed_max_parallel: int
    proof_max_parallel: int
    rate_limit_max_retries: int


@dataclass(frozen=True)
class EffectiveConfig:
    active_provider: str
    providers: dict[str, ProviderConfig]
    slots: dict[str, SlotConfig]
    scope: ScopeConfig
    validation_checks: tuple[ValidationCheck, ...]
    repo_memory: RepoMemoryConfig
    resources: ResourcesConfig
    github: GithubConfig
    swarm: SwarmConfig | None


@dataclass(frozen=True)
class LoadedConfig:
    effective: EffectiveConfig
    raw: dict[str, Any]
    sources: dict[PathKey, SourceInfo]
    config_path: Path
    resolved_env: dict[str, str]

    def source_label(self, *path: str) -> str:
        return self.sources[tuple(path)].label


def default_repo_config_path(cwd: Path | None = None) -> Path:
    base = cwd or Path.cwd()
    return base / "config" / "config.toml"


def default_repo_env_path(cwd: Path | None = None) -> Path:
    base = cwd or Path.cwd()
    return base / ".env"


def default_shared_resources_path(cwd: Path | None = None) -> Path:
    base = cwd or Path.cwd()
    return (base / "config" / "resources" / "shared").resolve()


def default_slot_resources_path(slot_name: str, cwd: Path | None = None) -> Path:
    if slot_name not in SLOT_NAMES:
        raise ConfigError(f"Unknown slot name for resources: {slot_name!r}")
    base = cwd or Path.cwd()
    return (base / "config" / "resources" / "slots" / slot_name).resolve()


def load_effective_config(
    *,
    cwd: Path | None = None,
    config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> LoadedConfig:
    cwd = cwd or Path.cwd()
    config_path = config_path or default_repo_config_path(cwd)
    environ = _resolve_runtime_env(cwd=cwd, env=env)

    if not config_path.exists():
        raise ConfigError(
            f"Missing required config at {config_path}. "
            "Run `awdit init-config` to scaffold a fresh config/config.toml before running awdit."
        )

    layers = [
        (BUILTIN_DEFAULTS, SourceInfo("built-in", None)),
        (_load_toml_file(config_path), SourceInfo("config", config_path.parent)),
    ]
    merged, sources = merge_layers(layers)
    effective = _normalize_and_validate(merged, sources, environ)
    return LoadedConfig(
        effective=effective,
        raw=merged,
        sources=sources,
        config_path=config_path,
        resolved_env=dict(environ),
    )


def apply_runtime_overrides(loaded: LoadedConfig, overrides: dict[str, Any]) -> LoadedConfig:
    return apply_runtime_overrides_with_env(loaded, overrides, env=os.environ)


def discover_resource_files(
    base_dir: Path,
    *,
    exclude: tuple[str, ...] = (),
) -> tuple[Path, ...]:
    if not base_dir.exists():
        return ()
    discovered: list[Path] = []
    for path in sorted(base_dir.rglob("*")):
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(base_dir)
        if _is_hidden_resource_path(relative):
            continue
        if _matches_any_glob(relative, exclude):
            continue
        discovered.append(path.resolve())
    return tuple(discovered)


def resolve_resource_section_items(
    base_dir: Path,
    config: ResourceSectionConfig,
) -> tuple[str, ...]:
    discovered = [str(path) for path in discover_resource_files(base_dir, exclude=config.exclude)]
    return tuple(discovered + list(config.include))


def apply_runtime_overrides_with_env(
    loaded: LoadedConfig,
    overrides: dict[str, Any],
    *,
    env: Mapping[str, str],
) -> LoadedConfig:
    if not overrides:
        return loaded
    merged, sources = merge_layer(
        loaded.raw,
        loaded.sources,
        overrides,
        SourceInfo("runtime override", None),
    )
    effective = _normalize_and_validate(merged, sources, env)
    return LoadedConfig(
        effective=effective,
        raw=merged,
        sources=sources,
        config_path=loaded.config_path,
        resolved_env=dict(env),
    )


def _resolve_runtime_env(
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
) -> dict[str, str]:
    base_env = dict(_load_dotenv_file(default_repo_env_path(cwd)))
    if env is None:
        base_env.update(os.environ)
    else:
        base_env.update(env)
    return base_env


def _load_dotenv_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    loaded: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        loaded[key] = value
    return loaded


def merge_patch_dicts(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged, _ = merge_layer(base, {}, patch, SourceInfo("runtime override", None))
    return merged


def save_repo_overrides(repo_config_path: Path, patch: dict[str, Any]) -> None:
    if not patch:
        return

    existing: dict[str, Any] = {}
    if repo_config_path.exists():
        existing = _load_toml_file(repo_config_path)

    merged = merge_patch_dicts(existing, patch)
    repo_config_path.parent.mkdir(parents=True, exist_ok=True)
    repo_config_path.write_text(_dump_known_schema_toml(merged), encoding="utf-8")


def summarize_config(loaded: LoadedConfig) -> list[tuple[str, str, str]]:
    active_provider = loaded.effective.active_provider
    provider = loaded.effective.providers[active_provider]
    rows = [
        ("Active provider", active_provider, loaded.source_label("active_provider")),
        (
            "Allowed models",
            ", ".join(provider.allowed_models),
            loaded.source_label("providers", active_provider, "allowed_models"),
        ),
    ]
    for slot_name in SLOT_NAMES:
        label = slot_name.replace("_", " ").title()
        rows.append(
            (
                f"{label} model",
                loaded.effective.slots[slot_name].default_model,
                loaded.source_label("slots", slot_name, "default_model"),
            )
        )
        effort = loaded.effective.slots[slot_name].reasoning_effort
        if effort is not None:
            rows.append(
                (
                    f"{label} reasoning effort",
                    effort,
                    loaded.source_label("slots", slot_name, "reasoning_effort"),
                )
            )
        rows.append(
            (
                f"{label} prompt",
                str(loaded.effective.slots[slot_name].prompt_file),
                loaded.source_label("slots", slot_name, "prompt_file"),
            )
        )
    rows.extend(
        [
            (
                "Scope include",
                ", ".join(loaded.effective.scope.include) or "(none)",
                loaded.source_label("scope", "include"),
            ),
            (
                "Scope exclude",
                ", ".join(loaded.effective.scope.exclude) or "(none)",
                loaded.source_label("scope", "exclude"),
            ),
            (
                "Validation checks",
                ", ".join(
                    f"{check.name} ({check.timeout_seconds}s)"
                    for check in loaded.effective.validation_checks
                ),
                loaded.source_label("validation", "checks"),
            ),
            (
                "Repo memory",
                "enabled" if loaded.effective.repo_memory.enabled else "disabled",
                loaded.source_label("repo_memory", "enabled"),
            ),
            (
                "Shared resource includes",
                ", ".join(loaded.effective.resources.shared.include) or "(none)",
                loaded.source_label("resources", "shared", "include"),
            ),
            (
                "Shared resource excludes",
                ", ".join(loaded.effective.resources.shared.exclude) or "(none)",
                loaded.source_label("resources", "shared", "exclude"),
            ),
            (
                "Prefer gh",
                str(loaded.effective.github.prefer_gh).lower(),
                loaded.source_label("github", "prefer_gh"),
            ),
        ]
    )
    if loaded.effective.swarm is not None:
        preset_source = loaded.sources.get(("swarm", "mode", "preset"))
        sweep_model_source = loaded.sources.get(("swarm", "models", "sweep"))
        proof_model_source = loaded.sources.get(("swarm", "models", "proof"))
        file_profile_source = loaded.sources.get(("swarm", "files", "profile"))
        budget_tokens_source = loaded.sources.get(("swarm", "budget", "tokens"))
        budget_mode_source = loaded.sources.get(("swarm", "budget", "mode"))
        danger_map_reasoning_source = loaded.sources.get(("swarm", "reasoning", "danger_map"))
        seed_reasoning_source = loaded.sources.get(("swarm", "reasoning", "seed"))
        proof_reasoning_source = loaded.sources.get(("swarm", "reasoning", "proof"))
        seed_max_parallel_source = loaded.sources.get(("swarm", "parallelism", "seed"))
        proof_max_parallel_source = loaded.sources.get(("swarm", "parallelism", "proof"))
        rate_limit_retries_source = loaded.sources.get(("swarm", "retries", "rate_limits"))
        rows.extend(
            [
                (
                    "Swarm preset",
                    loaded.effective.swarm.preset,
                    preset_source.label if preset_source else f"preset default ({loaded.effective.swarm.preset})",
                ),
                (
                    "Swarm sweep model",
                    loaded.effective.swarm.sweep_model,
                    sweep_model_source.label if sweep_model_source else f"preset default ({loaded.effective.swarm.preset})",
                ),
                (
                    "Swarm proof model",
                    loaded.effective.swarm.proof_model,
                    proof_model_source.label if proof_model_source else f"preset default ({loaded.effective.swarm.preset})",
                ),
                (
                    "Swarm file handling mode",
                    loaded.effective.swarm.eligible_file_profile,
                    file_profile_source.label if file_profile_source else f"preset default ({loaded.effective.swarm.preset})",
                ),
                (
                    "Swarm token budget",
                    str(loaded.effective.swarm.token_budget),
                    budget_tokens_source.label if budget_tokens_source else f"preset default ({loaded.effective.swarm.preset})",
                ),
                (
                    "Swarm budget mode",
                    loaded.effective.swarm.budget_mode,
                    budget_mode_source.label if budget_mode_source else f"preset default ({loaded.effective.swarm.preset})",
                ),
                (
                    "Swarm seed max parallel",
                    str(loaded.effective.swarm.seed_max_parallel),
                    seed_max_parallel_source.label
                    if seed_max_parallel_source
                    else f"preset default ({loaded.effective.swarm.preset})",
                ),
                (
                    "Swarm proof max parallel",
                    str(loaded.effective.swarm.proof_max_parallel),
                    proof_max_parallel_source.label
                    if proof_max_parallel_source
                    else f"preset default ({loaded.effective.swarm.preset})",
                ),
                (
                    "Swarm rate-limit retries",
                    str(loaded.effective.swarm.rate_limit_max_retries),
                    rate_limit_retries_source.label
                    if rate_limit_retries_source
                    else f"preset default ({loaded.effective.swarm.preset})",
                ),
                (
                    "Swarm danger-map reasoning",
                    loaded.effective.swarm.reasoning.danger_map,
                    danger_map_reasoning_source.label if danger_map_reasoning_source else "built-in default",
                ),
                (
                    "Swarm seed reasoning",
                    loaded.effective.swarm.reasoning.seed,
                    seed_reasoning_source.label if seed_reasoning_source else "built-in default",
                ),
                (
                    "Swarm proof reasoning",
                    loaded.effective.swarm.reasoning.proof,
                    proof_reasoning_source.label if proof_reasoning_source else "built-in default",
                ),
                (
                    "Swarm danger-map prompt",
                    str(loaded.effective.swarm.prompts.danger_map),
                    loaded.source_label("swarm", "prompts", "danger_map"),
                ),
                (
                    "Swarm seed prompt",
                    str(loaded.effective.swarm.prompts.seed),
                    loaded.source_label("swarm", "prompts", "seed"),
                ),
                (
                    "Swarm proof prompt",
                    str(loaded.effective.swarm.prompts.proof),
                    loaded.source_label("swarm", "prompts", "proof"),
                ),
            ]
        )
    return rows


def build_operational_save_patch(overrides: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in overrides.items() if key in CONFIG_BACKED_RUNTIME_KEYS}


def merge_layers(
    layers: list[tuple[dict[str, Any], SourceInfo]]
) -> tuple[dict[str, Any], dict[PathKey, SourceInfo]]:
    merged: dict[str, Any] = {}
    sources: dict[PathKey, SourceInfo] = {}
    for layer, source_info in layers:
        merged, sources = merge_layer(merged, sources, layer, source_info)
    return merged, sources


def merge_layer(
    base: dict[str, Any],
    base_sources: dict[PathKey, SourceInfo],
    patch: dict[str, Any],
    source_info: SourceInfo,
) -> tuple[dict[str, Any], dict[PathKey, SourceInfo]]:
    result = copy.deepcopy(base)
    sources = dict(base_sources)
    _merge_into(result, patch, source_info, (), sources)
    return result, sources


def _merge_into(
    target: dict[str, Any],
    patch: dict[str, Any],
    source_info: SourceInfo,
    path: PathKey,
    sources: dict[PathKey, SourceInfo],
) -> None:
    for key, value in patch.items():
        child_path = path + (key,)
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_into(target[key], value, source_info, child_path, sources)
            continue
        target[key] = copy.deepcopy(value)
        _mark_sources(value, child_path, source_info, sources)


def _mark_sources(
    value: Any,
    path: PathKey,
    source_info: SourceInfo,
    sources: dict[PathKey, SourceInfo],
) -> None:
    sources[path] = source_info
    if isinstance(value, dict):
        for key, child in value.items():
            _mark_sources(child, path + (key,), source_info, sources)


def _load_toml_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Failed to parse TOML at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Expected TOML table at {path}, found {type(data).__name__}.")
    return data


def _normalize_and_validate(
    raw: dict[str, Any],
    sources: dict[PathKey, SourceInfo],
    env: Mapping[str, str],
) -> EffectiveConfig:
    active_provider = _require_string(raw, ("active_provider",))
    providers_raw = _require_table(raw, ("providers",))
    if active_provider not in providers_raw:
        raise ConfigError(f"Active provider {active_provider!r} does not exist in [providers].")

    providers: dict[str, ProviderConfig] = {}
    for provider_name, provider_raw in providers_raw.items():
        if not isinstance(provider_raw, dict):
            raise ConfigError(f"Provider {provider_name!r} must be a table.")
        api_key_env = _require_string(provider_raw, ("providers", provider_name, "api_key_env"))
        base_url = _require_string(provider_raw, ("providers", provider_name, "base_url"))
        allowed_models = _require_string_list(
            provider_raw,
            ("providers", provider_name, "allowed_models"),
        )
        if not allowed_models:
            raise ConfigError(f"Provider {provider_name!r} must declare at least one allowed model.")
        providers[provider_name] = ProviderConfig(
            api_key_env=api_key_env,
            base_url=base_url,
            allowed_models=tuple(allowed_models),
        )

    active_provider_config = providers[active_provider]
    if not env.get(active_provider_config.api_key_env):
        raise ConfigError(
            f"Missing provider env var {active_provider_config.api_key_env!r} "
            f"for provider {active_provider!r}."
        )

    slots_raw = _require_table(raw, ("slots",))
    slots: dict[str, SlotConfig] = {}
    active_allowed = set(active_provider_config.allowed_models)
    for slot_name in SLOT_NAMES:
        if slot_name not in slots_raw or not isinstance(slots_raw[slot_name], dict):
            raise ConfigError(f"Missing required [slots.{slot_name}] config block.")
        slot_raw = slots_raw[slot_name]
        default_model = _require_string(slot_raw, ("slots", slot_name, "default_model"))
        if default_model not in active_allowed:
            raise ConfigError(
                f"Default model {default_model!r} for slot {slot_name!r} is not present in "
                f"providers.{active_provider}.allowed_models."
            )
        reasoning_effort = _optional_reasoning_effort(
            slot_raw,
            ("slots", slot_name, "reasoning_effort"),
        )
        prompt_value = _require_string(slot_raw, ("slots", slot_name, "prompt_file"))
        prompt_source = sources[("slots", slot_name, "prompt_file")]
        prompt_path = _resolve_declared_path(prompt_value, prompt_source)
        if not prompt_path.exists():
            raise ConfigError(f"Missing prompt file for slot {slot_name!r}: {prompt_path}")
        slots[slot_name] = SlotConfig(
            default_model=default_model,
            reasoning_effort=reasoning_effort,
            prompt_file=prompt_path,
        )

    scope_raw = _require_table(raw, ("scope",))
    scope = ScopeConfig(
        include=tuple(_require_string_list(scope_raw, ("scope", "include"))),
        exclude=tuple(_require_string_list(scope_raw, ("scope", "exclude"))),
    )

    validation_raw = _require_table(raw, ("validation",))
    checks_raw = validation_raw.get("checks")
    if not isinstance(checks_raw, list) or not checks_raw:
        raise ConfigError("validation.checks must be a non-empty list.")
    checks: list[ValidationCheck] = []
    for index, item in enumerate(checks_raw):
        if not isinstance(item, dict):
            raise ConfigError(f"validation.checks[{index}] must be a table.")
        name = _require_string(item, ("validation", "checks", str(index), "name"))
        command = _require_string(item, ("validation", "checks", str(index), "command"))
        timeout_seconds = _require_positive_int(
            item,
            ("validation", "checks", str(index), "timeout_seconds"),
        )
        checks.append(
            ValidationCheck(name=name, command=command, timeout_seconds=timeout_seconds)
        )

    repo_memory_raw = _require_table(raw, ("repo_memory",))
    repo_memory = RepoMemoryConfig(
        enabled=_require_bool(repo_memory_raw, ("repo_memory", "enabled")),
        require_danger_map_approval=_require_bool(
            repo_memory_raw,
            ("repo_memory", "require_danger_map_approval"),
        ),
        confirm_refresh_on_startup=_require_bool(
            repo_memory_raw,
            ("repo_memory", "confirm_refresh_on_startup"),
        ),
        auto_update_on_completion=_require_bool(
            repo_memory_raw,
            ("repo_memory", "auto_update_on_completion"),
        ),
    )

    resources_raw = _require_table(raw, ("resources",))
    shared_resources_raw = _require_table(resources_raw, ("shared",))
    shared_resources = ResourceSectionConfig(
        include=tuple(_require_string_list(shared_resources_raw, ("resources", "shared", "include"))),
        exclude=tuple(_require_string_list(shared_resources_raw, ("resources", "shared", "exclude"))),
    )
    slots_resources_raw = _require_table(resources_raw, ("slots",))
    slot_resources: dict[str, ResourceSectionConfig] = {}
    for slot_name in SLOT_NAMES:
        slot_raw = _require_table(slots_resources_raw, (slot_name,))
        slot_resources[slot_name] = ResourceSectionConfig(
            include=tuple(
                _require_string_list(slot_raw, ("resources", "slots", slot_name, "include"))
            ),
            exclude=tuple(
                _require_string_list(slot_raw, ("resources", "slots", slot_name, "exclude"))
            ),
        )

    github_raw = raw.get("github", {})
    if not isinstance(github_raw, dict):
        raise ConfigError("github must be a table.")
    prefer_gh = github_raw.get("prefer_gh", True)
    if not isinstance(prefer_gh, bool):
        raise ConfigError("github.prefer_gh must be true or false.")

    swarm_raw = raw.get("swarm", {})
    if swarm_raw is None:
        swarm_raw = {}
    if not isinstance(swarm_raw, dict):
        raise ConfigError("swarm must be a table.")

    swarm: SwarmConfig | None = None
    if swarm_raw:
        legacy_keys = sorted(SWARM_LEGACY_KEYS & set(swarm_raw))
        if legacy_keys:
            raise ConfigError(
                "Legacy swarm schema is no longer supported. "
                "Expected grouped sections like [swarm.mode], [swarm.models], [swarm.files], "
                "[swarm.budget], [swarm.parallelism], [swarm.retries], [swarm.reasoning], "
                f"and [swarm.prompts]. Remove legacy keys: {', '.join(legacy_keys)}. "
                "Run `awdit init-config` to regenerate a fresh grouped scaffold."
            )
        if "prompt_file" in swarm_raw:
            raise ConfigError(
                "swarm.prompt_file is no longer supported. "
                "Use [swarm.prompts] with danger_map, seed, and proof entries."
            )

        mode_raw = _optional_table(swarm_raw, ("swarm", "mode"))
        preset = _optional_choice(mode_raw, ("swarm", "mode", "preset"), SWARM_PRESET_VALUES)
        preset = preset or DEFAULT_SWARM_PRESET
        preset_defaults = SWARM_PRESET_DEFAULTS[preset]

        prompts_raw = _require_table(raw, ("swarm", "prompts"))

        danger_map_prompt_value = _require_string(prompts_raw, ("swarm", "prompts", "danger_map"))
        danger_map_prompt_source = sources[("swarm", "prompts", "danger_map")]
        danger_map_prompt_path = _resolve_declared_path(
            danger_map_prompt_value,
            danger_map_prompt_source,
        )
        if not danger_map_prompt_path.exists():
            raise ConfigError(f"Missing prompt file for swarm: {danger_map_prompt_path}")

        seed_prompt_value = _require_string(prompts_raw, ("swarm", "prompts", "seed"))
        seed_prompt_source = sources[("swarm", "prompts", "seed")]
        seed_prompt_path = _resolve_declared_path(seed_prompt_value, seed_prompt_source)
        if not seed_prompt_path.exists():
            raise ConfigError(f"Missing prompt file for swarm: {seed_prompt_path}")

        proof_prompt_value = _require_string(prompts_raw, ("swarm", "prompts", "proof"))
        proof_prompt_source = sources[("swarm", "prompts", "proof")]
        proof_prompt_path = _resolve_declared_path(proof_prompt_value, proof_prompt_source)
        if not proof_prompt_path.exists():
            raise ConfigError(f"Missing prompt file for swarm: {proof_prompt_path}")

        models_raw = _optional_table(swarm_raw, ("swarm", "models"))
        sweep_model = _optional_string(models_raw, ("swarm", "models", "sweep")) or preset_defaults["sweep_model"]
        proof_model = _optional_string(models_raw, ("swarm", "models", "proof")) or preset_defaults["proof_model"]
        if sweep_model not in active_allowed:
            raise ConfigError(
                f"Swarm sweep model {sweep_model!r} is not present in "
                f"providers.{active_provider}.allowed_models."
            )
        if proof_model not in active_allowed:
            raise ConfigError(
                f"Swarm proof model {proof_model!r} is not present in "
                f"providers.{active_provider}.allowed_models."
            )

        files_raw = _optional_table(swarm_raw, ("swarm", "files"))
        eligible_file_profile = (
            _optional_choice(files_raw, ("swarm", "files", "profile"), SWARM_ELIGIBLE_FILE_PROFILES)
            or preset_defaults["eligible_file_profile"]
        )

        budget_raw = _optional_table(swarm_raw, ("swarm", "budget"))
        token_budget = (
            _optional_positive_int(budget_raw, ("swarm", "budget", "tokens"))
            or preset_defaults["token_budget"]
        )
        budget_mode = (
            _optional_choice(budget_raw, ("swarm", "budget", "mode"), SWARM_BUDGET_MODE_VALUES)
            or preset_defaults["budget_mode"]
        )

        parallel_raw = _optional_table(swarm_raw, ("swarm", "parallelism"))
        seed_max_parallel = (
            _optional_positive_int(parallel_raw, ("swarm", "parallelism", "seed"))
            or preset_defaults["seed_max_parallel"]
        )
        proof_max_parallel = (
            _optional_positive_int(parallel_raw, ("swarm", "parallelism", "proof"))
            or preset_defaults["proof_max_parallel"]
        )

        retries_raw = _optional_table(swarm_raw, ("swarm", "retries"))
        rate_limit_max_retries = (
            _optional_positive_int(retries_raw, ("swarm", "retries", "rate_limits"))
            or preset_defaults["rate_limit_max_retries"]
        )

        reasoning_raw = _optional_table(swarm_raw, ("swarm", "reasoning"))
        swarm = SwarmConfig(
            prompts=SwarmPromptsConfig(
                danger_map=danger_map_prompt_path,
                seed=seed_prompt_path,
                proof=proof_prompt_path,
            ),
            reasoning=SwarmReasoningConfig(
                danger_map=_optional_reasoning_effort(
                    reasoning_raw,
                    ("swarm", "reasoning", "danger_map"),
                )
                or DEFAULT_SWARM_REASONING_EFFORTS["danger_map"],
                seed=_optional_reasoning_effort(
                    reasoning_raw,
                    ("swarm", "reasoning", "seed"),
                )
                or DEFAULT_SWARM_REASONING_EFFORTS["seed"],
                proof=_optional_reasoning_effort(
                    reasoning_raw,
                    ("swarm", "reasoning", "proof"),
                )
                or DEFAULT_SWARM_REASONING_EFFORTS["proof"],
            ),
            preset=preset,
            sweep_model=sweep_model,
            proof_model=proof_model,
            eligible_file_profile=eligible_file_profile,
            token_budget=token_budget,
            budget_mode=budget_mode,
            seed_max_parallel=seed_max_parallel,
            proof_max_parallel=proof_max_parallel,
            rate_limit_max_retries=rate_limit_max_retries,
        )

    return EffectiveConfig(
        active_provider=active_provider,
        providers=providers,
        slots=slots,
        scope=scope,
        validation_checks=tuple(checks),
        repo_memory=repo_memory,
        resources=ResourcesConfig(shared=shared_resources, slots=slot_resources),
        github=GithubConfig(prefer_gh=prefer_gh),
        swarm=swarm,
    )


def _require_table(container: dict[str, Any], path: PathKey) -> dict[str, Any]:
    if len(path) == 1:
        value = container.get(path[0])
    else:
        value = container
        for key in path:
            if not isinstance(value, dict):
                break
            value = value.get(key)
    if not isinstance(value, dict):
        dotted = ".".join(path)
        raise ConfigError(f"Missing or invalid table for {dotted}.")
    return value


def _require_string(container: dict[str, Any], path: PathKey) -> str:
    dotted = ".".join(path)
    value = container.get(path[-1])
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Missing or invalid string for {dotted}.")
    return value


def _require_string_list(container: dict[str, Any], path: PathKey) -> list[str]:
    dotted = ".".join(path)
    value = container.get(path[-1])
    if not isinstance(value, list):
        raise ConfigError(f"Missing or invalid list for {dotted}.")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"Every item in {dotted} must be a non-empty string.")
        normalized.append(item)
    return normalized


def _require_positive_int(container: dict[str, Any], path: PathKey) -> int:
    dotted = ".".join(path)
    value = container.get(path[-1])
    if not isinstance(value, int) or value <= 0:
        raise ConfigError(f"Missing or invalid positive integer for {dotted}.")
    return value


def _require_bool(container: dict[str, Any], path: PathKey) -> bool:
    dotted = ".".join(path)
    value = container.get(path[-1])
    if not isinstance(value, bool):
        raise ConfigError(f"Missing or invalid boolean for {dotted}.")
    return value


def _optional_table(container: dict[str, Any] | None, path: PathKey) -> dict[str, Any]:
    if container is None:
        return {}
    value = container.get(path[-1])
    if value is None:
        return {}
    if not isinstance(value, dict):
        dotted = ".".join(path)
        raise ConfigError(f"{dotted} must be a table.")
    return value


def _optional_string(container: dict[str, Any] | None, path: PathKey) -> str | None:
    if container is None:
        return None
    value = container.get(path[-1])
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        dotted = ".".join(path)
        raise ConfigError(f"{dotted} must be a non-empty string.")
    return value


def _optional_positive_int(container: dict[str, Any], path: PathKey) -> int | None:
    if container is None:
        return None
    value = container.get(path[-1])
    if value is None:
        return None
    if not isinstance(value, int) or value <= 0:
        dotted = ".".join(path)
        raise ConfigError(f"{dotted} must be a positive integer.")
    return value


def _optional_reasoning_effort(container: dict[str, Any], path: PathKey) -> str | None:
    if container is None:
        return None
    value = container.get(path[-1])
    if value is None:
        return None
    if not isinstance(value, str) or value not in REASONING_EFFORT_VALUES:
        dotted = ".".join(path)
        allowed = ", ".join(REASONING_EFFORT_VALUES)
        raise ConfigError(f"{dotted} must be one of: {allowed}.")
    return value


def _optional_choice(
    container: dict[str, Any] | None,
    path: PathKey,
    allowed_values: tuple[str, ...],
) -> str | None:
    if container is None:
        return None
    value = container.get(path[-1])
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed_values:
        dotted = ".".join(path)
        allowed = ", ".join(allowed_values)
        raise ConfigError(f"{dotted} must be one of: {allowed}.")
    return value


def _matches_any_glob(relative_path: Path, patterns: tuple[str, ...]) -> bool:
    posix_path = PurePosixPath(relative_path.as_posix())
    return any(posix_path.match(pattern) for pattern in patterns)


def _is_hidden_resource_path(relative_path: Path) -> bool:
    return any(part.startswith(".") for part in relative_path.parts)


def _resolve_declared_path(value: str, source_info: SourceInfo) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    if source_info.base_dir is None:
        return path.resolve()
    return (source_info.base_dir / path).resolve()


def _dump_known_schema_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []

    if "active_provider" in data:
        lines.append(f'active_provider = {_format_toml_value(data["active_provider"])}')

    providers = data.get("providers", {})
    if isinstance(providers, dict):
        for provider_name in sorted(providers):
            provider = providers[provider_name]
            if not isinstance(provider, dict):
                continue
            lines.extend(
                [
                    "",
                    f"[providers.{provider_name}]",
                ]
            )
            for key in ("api_key_env", "base_url", "allowed_models"):
                if key in provider:
                    lines.append(f"{key} = {_format_toml_value(provider[key])}")

    slots = data.get("slots", {})
    if isinstance(slots, dict):
        for slot_name in SLOT_NAMES:
            if slot_name not in slots or not isinstance(slots[slot_name], dict):
                continue
            slot = slots[slot_name]
            lines.extend(["", f"[slots.{slot_name}]"])
            for key in ("default_model", "reasoning_effort", "prompt_file"):
                if key in slot:
                    lines.append(f"{key} = {_format_toml_value(slot[key])}")

    scope = data.get("scope", {})
    if isinstance(scope, dict) and scope:
        lines.extend(["", "[scope]"])
        for key in ("include", "exclude"):
            if key in scope:
                lines.append(f"{key} = {_format_toml_value(scope[key])}")

    validation = data.get("validation", {})
    if isinstance(validation, dict):
        checks = validation.get("checks", [])
        if isinstance(checks, list):
            for check in checks:
                if not isinstance(check, dict):
                    continue
                lines.extend(["", "[[validation.checks]]"])
                for key in ("name", "command", "timeout_seconds"):
                    if key in check:
                        lines.append(f"{key} = {_format_toml_value(check[key])}")

    repo_memory = data.get("repo_memory", {})
    if isinstance(repo_memory, dict) and repo_memory:
        lines.extend(["", "[repo_memory]"])
        for key in (
            "enabled",
            "require_danger_map_approval",
            "confirm_refresh_on_startup",
            "auto_update_on_completion",
        ):
            if key in repo_memory:
                lines.append(f"{key} = {_format_toml_value(repo_memory[key])}")

    resources = data.get("resources", {})
    if isinstance(resources, dict):
        shared = resources.get("shared")
        if isinstance(shared, dict):
            lines.extend(["", "[resources.shared]"])
            for key in ("include", "exclude"):
                if key in shared:
                    lines.append(f"{key} = {_format_toml_value(shared[key])}")

        slots_resources = resources.get("slots", {})
        if isinstance(slots_resources, dict):
            for slot_name in SLOT_NAMES:
                slot_resource = slots_resources.get(slot_name)
                if not isinstance(slot_resource, dict):
                    continue
                lines.extend(["", f"[resources.slots.{slot_name}]"])
                for key in ("include", "exclude"):
                    if key in slot_resource:
                        lines.append(f"{key} = {_format_toml_value(slot_resource[key])}")

    github = data.get("github", {})
    if isinstance(github, dict) and github:
        lines.extend(["", "[github]"])
        if "prefer_gh" in github:
            lines.append(f"prefer_gh = {_format_toml_value(github['prefer_gh'])}")

    swarm = data.get("swarm", {})
    if isinstance(swarm, dict) and swarm:
        mode = swarm.get("mode")
        if isinstance(mode, dict) and mode:
            lines.extend(["", "[swarm.mode]"])
            if "preset" in mode:
                lines.append(f"preset = {_format_toml_value(mode['preset'])}")
        models = swarm.get("models")
        if isinstance(models, dict) and models:
            lines.extend(["", "[swarm.models]"])
            for key in ("sweep", "proof"):
                if key in models:
                    lines.append(f"{key} = {_format_toml_value(models[key])}")
        files = swarm.get("files")
        if isinstance(files, dict) and files:
            lines.extend(["", "[swarm.files]"])
            if "profile" in files:
                lines.append(f"profile = {_format_toml_value(files['profile'])}")
        budget = swarm.get("budget")
        if isinstance(budget, dict) and budget:
            lines.extend(["", "[swarm.budget]"])
            for key in ("tokens", "mode"):
                if key in budget:
                    lines.append(f"{key} = {_format_toml_value(budget[key])}")
        parallelism = swarm.get("parallelism")
        if isinstance(parallelism, dict) and parallelism:
            lines.extend(["", "[swarm.parallelism]"])
            for key in ("seed", "proof"):
                if key in parallelism:
                    lines.append(f"{key} = {_format_toml_value(parallelism[key])}")
        retries = swarm.get("retries")
        if isinstance(retries, dict) and retries:
            lines.extend(["", "[swarm.retries]"])
            if "rate_limits" in retries:
                lines.append(f"rate_limits = {_format_toml_value(retries['rate_limits'])}")
        reasoning = swarm.get("reasoning")
        if isinstance(reasoning, dict) and reasoning:
            lines.extend(["", "[swarm.reasoning]"])
            for key in ("danger_map", "seed", "proof"):
                if key in reasoning:
                    lines.append(f"{key} = {_format_toml_value(reasoning[key])}")
        prompts = swarm.get("prompts")
        if isinstance(prompts, dict) and prompts:
            lines.extend(["", "[swarm.prompts]"])
            for key in ("danger_map", "seed", "proof"):
                if key in prompts:
                    lines.append(f"{key} = {_format_toml_value(prompts[key])}")

    return "\n".join(lines).strip() + "\n"


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_format_toml_value(item) for item in value) + "]"
    raise TypeError(f"Unsupported TOML value type: {type(value).__name__}")


def render_config_scaffold() -> str:
    slot_models = {
        "hunter_1": "gpt-5.4-mini",
        "hunter_2": "gpt-5.4-mini",
        "skeptic_1": "gpt-5.4",
        "skeptic_2": "gpt-5.4-mini",
        "referee_1": "gpt-5.4",
        "referee_2": "gpt-5.4-mini",
        "solver_1": "gpt-5.4",
        "solver_2": "gpt-5.4-mini",
    }
    slot_reasoning = {
        "hunter_1": "low",
        "hunter_2": "low",
        "skeptic_1": "medium",
        "skeptic_2": "low",
        "referee_1": "medium",
        "referee_2": "low",
        "solver_1": "medium",
        "solver_2": "low",
    }
    slot_blocks: list[str] = []
    for slot_name in SLOT_NAMES:
        slot_blocks.append(
            textwrap.dedent(
                f"""
                [slots.{slot_name}]
                default_model = "{slot_models[slot_name]}"
                reasoning_effort = "{slot_reasoning[slot_name]}"
                prompt_file = "prompts/{slot_name}.md"
                """
            ).strip()
        )

    return (
        textwrap.dedent(
            """
            # Repo-scoped awdit config scaffold.
            # This file is intentionally verbose and self-documenting.
            # Optional sections say when they may be omitted and list every accepted value nearby.
            # Recommended path: keep the safe swarm preset and only override grouped swarm sections you truly need.

            active_provider = "openai"

            [providers.openai]
            api_key_env = "OPENAI_API_KEY"
            base_url = "https://api.openai.com/v1"
            allowed_models = ["gpt-5.4", "gpt-5.4-mini"]

            [scope]
            include = [
              "src/**",
              "tests/**",
              "config/prompts/**",
              "config/config.toml",
              "config/config.toml.example",
              "pyproject.toml",
              "uv.lock",
              ".gitignore",
            ]
            exclude = [
              "src/__pycache__/**",
              "tests/__pycache__/**",
              "src/*.egg-info/**",
              "config/resources/**",
              "docs/**",
              ".env",
              ".env.example",
            ]

            [[validation.checks]]
            name = "pytest"
            command = "pytest -q"
            timeout_seconds = 600

            [repo_memory]
            enabled = true
            require_danger_map_approval = true
            confirm_refresh_on_startup = true
            auto_update_on_completion = true

            # Optional.
            # config/resources/shared/ is auto-included by discovery unless excluded here.
            # Options:
            #   - include: explicit URLs or out-of-tree paths only
            #   - exclude: glob patterns relative to config/resources/shared/
            [resources.shared]
            include = [
              "../docs/architecture.md",
              "../docs/agent-isolation-workflow.md",
            ]
            exclude = []

            # Optional per-slot resource overrides.
            # Each block may be omitted, but when present it accepts:
            #   - include: explicit URLs or out-of-tree paths only
            #   - exclude: glob patterns relative to config/resources/slots/<slot>/
            """
        ).strip()
        + "\n\n"
        + "\n\n".join(
            textwrap.dedent(
                f"""
                [resources.slots.{slot_name}]
                include = []
                exclude = []
                """
            ).strip()
            for slot_name in SLOT_NAMES
        )
        + "\n\n"
        + textwrap.dedent(
            """
            [github]
            prefer_gh = true

            # Optional.
            # If omitted entirely, awdit does not enable swarm mode for this repo.
            # Grouped schema only: legacy flat swarm keys are rejected.

            [swarm.mode]
            # Optional. Omit to use the default preset.
            # Options:
            #   - "safe": default, stability-first, hard-safe continuation and launch gating
            #   - "balanced": still hard-safe, but reopens safe concurrency sooner
            #   - "fast": favors throughput and may knowingly accept retry churn
            preset = "safe"

            [swarm.models]
            # Optional overrides for the chosen preset.
            # Options:
            #   - sweep: any model listed in providers.<active>.allowed_models
            #   - proof: any model listed in providers.<active>.allowed_models
            sweep = "gpt-5.4-mini"
            proof = "gpt-5.4-mini"

            [swarm.files]
            # Optional.
            # Options:
            #   - "code_config_tests": code, config, and test files only
            #   - "pr_changed_files": one worker per changed file in the current branch diff
            profile = "code_config_tests"

            [swarm.budget]
            # Optional.
            # Options:
            #   - tokens: positive integer >= 1
            #   - mode:
            #       - "enforced": scheduler blocks or degrades oversized work to stay inside the budget
            #       - "advisory": budget is reported, but faster presets may push harder
            tokens = 120000
            mode = "enforced"

            [swarm.parallelism]
            # Optional.
            # Omit either value to inherit the preset default.
            # Options:
            #   - seed: positive integer >= 1
            #   - proof: positive integer >= 1
            seed = 2
            proof = 1

            [swarm.retries]
            # Optional.
            # Options:
            #   - rate_limits: positive integer >= 1
            rate_limits = 3

            [swarm.reasoning]
            # Optional.
            # Omit any individual value to inherit the built-in default for that stage.
            # Options for every field: "low", "medium", "high"
            danger_map = "medium"
            seed = "low"
            proof = "medium"

            [swarm.prompts]
            # Required when swarm is enabled.
            # Options:
            #   - danger_map: relative or absolute path to the danger-map prompt
            #   - seed: relative or absolute path to the seed prompt
            #   - proof: relative or absolute path to the proof prompt
            danger_map = "prompts/swarm_danger_map.md"
            seed = "prompts/swarm_seed.md"
            proof = "prompts/swarm_proof.md"
            """
        ).strip()
        + "\n\n"
        + "\n\n".join(slot_blocks)
        + "\n"
    )

from __future__ import annotations

import copy
import json
import os
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
SWARM_ELIGIBLE_FILE_PROFILES = ("code_config_tests", "all_tracked")
DEFAULT_SWARM_REASONING_EFFORTS = {
    "danger_map": "high",
    "seed": "low",
    "proof": "medium",
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
    sweep_model: str
    proof_model: str
    eligible_file_profile: str
    token_budget: int
    allow_no_limit: bool


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
            "Create config/config.toml before running awdit."
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
        danger_map_reasoning_source = loaded.sources.get(("swarm", "reasoning", "danger_map"))
        seed_reasoning_source = loaded.sources.get(("swarm", "reasoning", "seed"))
        proof_reasoning_source = loaded.sources.get(("swarm", "reasoning", "proof"))
        rows.extend(
            [
                (
                    "Swarm sweep model",
                    loaded.effective.swarm.sweep_model,
                    loaded.source_label("swarm", "sweep_model"),
                ),
                (
                    "Swarm proof model",
                    loaded.effective.swarm.proof_model,
                    loaded.source_label("swarm", "proof_model"),
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
        if "prompt_file" in swarm_raw:
            raise ConfigError(
                "swarm.prompt_file is no longer supported. "
                "Use [swarm.prompts] with danger_map, seed, and proof entries."
            )

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

        sweep_model = _require_string(swarm_raw, ("swarm", "sweep_model"))
        proof_model = _require_string(swarm_raw, ("swarm", "proof_model"))
        if sweep_model not in active_allowed:
            raise ConfigError(
                f"Swarm sweep_model {sweep_model!r} is not present in "
                f"providers.{active_provider}.allowed_models."
            )
        if proof_model not in active_allowed:
            raise ConfigError(
                f"Swarm proof_model {proof_model!r} is not present in "
                f"providers.{active_provider}.allowed_models."
            )

        eligible_file_profile = _require_string(swarm_raw, ("swarm", "eligible_file_profile"))
        if eligible_file_profile not in SWARM_ELIGIBLE_FILE_PROFILES:
            allowed = ", ".join(SWARM_ELIGIBLE_FILE_PROFILES)
            raise ConfigError(f"swarm.eligible_file_profile must be one of: {allowed}.")

        reasoning_raw = swarm_raw.get("reasoning", {})
        if reasoning_raw is None:
            reasoning_raw = {}
        if not isinstance(reasoning_raw, dict):
            raise ConfigError("swarm.reasoning must be a table.")

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
            sweep_model=sweep_model,
            proof_model=proof_model,
            eligible_file_profile=eligible_file_profile,
            token_budget=_require_positive_int(swarm_raw, ("swarm", "token_budget")),
            allow_no_limit=_require_bool(swarm_raw, ("swarm", "allow_no_limit")),
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


def _optional_reasoning_effort(container: dict[str, Any], path: PathKey) -> str | None:
    value = container.get(path[-1])
    if value is None:
        return None
    if not isinstance(value, str) or value not in REASONING_EFFORT_VALUES:
        dotted = ".".join(path)
        allowed = ", ".join(REASONING_EFFORT_VALUES)
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
        lines.extend(["", "[swarm]"])
        for key in ("sweep_model", "proof_model", "eligible_file_profile", "token_budget", "allow_no_limit"):
            if key in swarm:
                lines.append(f"{key} = {_format_toml_value(swarm[key])}")
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

from __future__ import annotations

import sqlite3
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from config import (
    ConfigError,
    apply_runtime_overrides_with_env,
    default_repo_env_path,
    default_shared_resources_path,
    default_slot_resources_path,
    discover_resource_files,
    load_effective_config,
    resolve_resource_section_items,
    save_repo_overrides,
)
from repo_memory import legacy_repo_key, migrate_legacy_repo_memory_dir, resolve_repo_identity
from state_db import (
    ensure_state_db,
    insert_run,
    load_learned_model_limit,
    record_run_failure,
    save_learned_model_limit,
    update_run_status,
)


ALL_SLOTS = (
    "hunter_1",
    "hunter_2",
    "skeptic_1",
    "skeptic_2",
    "referee_1",
    "referee_2",
    "solver_1",
    "solver_2",
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def _write_prompt_tree(base: Path, prefix: str = "") -> None:
    prompt_dir = base / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    for slot in ALL_SLOTS:
        (prompt_dir / f"{prefix}{slot}.md").write_text(f"# {slot}\n", encoding="utf-8")
    (prompt_dir / "swarm_danger_map.md").write_text("# swarm danger map\n", encoding="utf-8")
    (prompt_dir / "swarm_seed.md").write_text("# swarm seed\n", encoding="utf-8")
    (prompt_dir / "swarm_proof.md").write_text("# swarm proof\n", encoding="utf-8")


def _user_config_text() -> str:
    slot_blocks = []
    for slot in ALL_SLOTS:
        default_model = "gpt-5.4-mini" if slot in {"skeptic_2", "solver_2"} else "gpt-5.4"
        slot_blocks.append(
            f"""
            [slots.{slot}]
            default_model = "{default_model}"
            reasoning_effort = "medium"
            prompt_file = "prompts/{slot}.md"
            """
        )
    return (
        """
        active_provider = "openai"

        [providers.openai]
        api_key_env = "OPENAI_API_KEY"
        base_url = "https://api.openai.com/v1"
        allowed_models = ["gpt-5.4", "gpt-5.4-mini"]

        [scope]
        include = ["app/**", "tests/**"]
        exclude = ["docs/**"]

        [[validation.checks]]
        name = "pytest"
        command = "pytest -q"
        timeout_seconds = 600

        [[validation.checks]]
        name = "ruff"
        command = "ruff check ."
        timeout_seconds = 300

        [repo_memory]
        enabled = true
        require_danger_map_approval = true
        confirm_refresh_on_startup = true
        auto_update_on_completion = true

        [resources.shared]
        exclude = ["drafts/**"]

        [resources.slots.hunter_1]
        exclude = ["archive/**"]

        [github]
        prefer_gh = true

        [swarm.mode]
        preset = "safe"

        [swarm.models]
        sweep = "gpt-5.4-mini"
        proof = "gpt-5.4"

        [swarm.files]
        profile = "code_config_tests"

        [swarm.budget]
        tokens = 120000
        mode = "enforced"

        [swarm.prompts]
        danger_map = "prompts/swarm_danger_map.md"
        seed = "prompts/swarm_seed.md"
        proof = "prompts/swarm_proof.md"
        """
        + "\n".join(slot_blocks)
    )


class ConfigTests(unittest.TestCase):
    def test_repo_config_loads_declared_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            repo_config = repo_dir / "config" / "config.toml"

            repo_prompt_dir = repo_dir / "config" / "prompts"
            repo_prompt_dir.mkdir(parents=True, exist_ok=True)
            (repo_prompt_dir / "hunter-1.md").write_text("# hunter override\n", encoding="utf-8")
            _write_prompt_tree(repo_dir / "config")
            _write(
                repo_config,
                """
                active_provider = "openai"

                [providers.openai]
                api_key_env = "OPENAI_API_KEY"
                base_url = "https://api.openai.com/v1"
                allowed_models = ["gpt-5.4", "gpt-5.4-mini"]

                [scope]
                include = ["src/**"]
                exclude = ["fixtures/**"]

                [[validation.checks]]
                name = "unit"
                command = "pytest -q"
                timeout_seconds = 120

                [repo_memory]
                enabled = true
                require_danger_map_approval = true
                confirm_refresh_on_startup = true
                auto_update_on_completion = true

                [resources.shared]
                exclude = ["legacy/**"]

                [resources.slots.hunter_1]
                exclude = ["old/**"]

                [github]
                prefer_gh = true

                [swarm.mode]
                preset = "safe"

                [swarm.models]
                sweep = "gpt-5.4-mini"
                proof = "gpt-5.4"

                [swarm.files]
                profile = "code_config_tests"

                [swarm.budget]
                tokens = 120000
                mode = "advisory"

                [swarm.parallelism]
                seed = 4
                proof = 2

                [swarm.retries]
                rate_limits = 5

                [swarm.reasoning]
                danger_map = "low"
                seed = "high"
                proof = "medium"

                [swarm.prompts]
                danger_map = "prompts/swarm_danger_map.md"
                seed = "prompts/swarm_seed.md"
                proof = "prompts/swarm_proof.md"

                [slots.hunter_1]
                default_model = "gpt-5.4-mini"
                reasoning_effort = "high"
                prompt_file = "prompts/hunter-1.md"

                [slots.hunter_2]
                default_model = "gpt-5.4"
                reasoning_effort = "medium"
                prompt_file = "prompts/hunter_2.md"

                [slots.skeptic_1]
                default_model = "gpt-5.4"
                reasoning_effort = "medium"
                prompt_file = "prompts/skeptic_1.md"

                [slots.skeptic_2]
                default_model = "gpt-5.4-mini"
                reasoning_effort = "low"
                prompt_file = "prompts/skeptic_2.md"

                [slots.referee_1]
                default_model = "gpt-5.4"
                reasoning_effort = "medium"
                prompt_file = "prompts/referee_1.md"

                [slots.referee_2]
                default_model = "gpt-5.4"
                reasoning_effort = "medium"
                prompt_file = "prompts/referee_2.md"

                [slots.solver_1]
                default_model = "gpt-5.4"
                reasoning_effort = "medium"
                prompt_file = "prompts/solver_1.md"

                [slots.solver_2]
                default_model = "gpt-5.4-mini"
                reasoning_effort = "low"
                prompt_file = "prompts/solver_2.md"
                """,
            )

            loaded = load_effective_config(
                cwd=repo_dir,
                config_path=repo_config,
                env={"OPENAI_API_KEY": "token"},
            )

            self.assertEqual(
                ("gpt-5.4", "gpt-5.4-mini"),
                loaded.effective.providers["openai"].allowed_models,
            )
            self.assertEqual("gpt-5.4-mini", loaded.effective.slots["hunter_1"].default_model)
            self.assertEqual("high", loaded.effective.slots["hunter_1"].reasoning_effort)
            self.assertEqual(
                (repo_prompt_dir / "hunter-1.md").resolve(),
                loaded.effective.slots["hunter_1"].prompt_file,
            )
            self.assertEqual("config", loaded.source_label("slots", "hunter_1", "prompt_file"))
            self.assertEqual("config", loaded.source_label("slots", "hunter_2", "prompt_file"))
            self.assertEqual(("src/**",), loaded.effective.scope.include)
            self.assertEqual(("fixtures/**",), loaded.effective.scope.exclude)
            self.assertEqual(
                (),
                loaded.effective.resources.shared.include,
            )
            self.assertEqual(("legacy/**",), loaded.effective.resources.shared.exclude)
            self.assertEqual(
                (),
                loaded.effective.resources.slots["hunter_1"].include,
            )
            self.assertEqual(("old/**",), loaded.effective.resources.slots["hunter_1"].exclude)
            self.assertIsNotNone(loaded.effective.swarm)
            self.assertEqual("safe", loaded.effective.swarm.preset)
            self.assertEqual("gpt-5.4-mini", loaded.effective.swarm.sweep_model)
            self.assertEqual("gpt-5.4", loaded.effective.swarm.proof_model)
            self.assertEqual("advisory", loaded.effective.swarm.budget_mode)
            self.assertEqual(4, loaded.effective.swarm.seed_max_parallel)
            self.assertEqual(2, loaded.effective.swarm.proof_max_parallel)
            self.assertEqual(5, loaded.effective.swarm.rate_limit_max_retries)
            self.assertEqual("low", loaded.effective.swarm.reasoning.danger_map)
            self.assertEqual("high", loaded.effective.swarm.reasoning.seed)
            self.assertEqual("medium", loaded.effective.swarm.reasoning.proof)
            self.assertEqual(
                (repo_prompt_dir / "swarm_danger_map.md").resolve(),
                loaded.effective.swarm.prompts.danger_map,
            )
            self.assertEqual(
                (repo_prompt_dir / "swarm_seed.md").resolve(),
                loaded.effective.swarm.prompts.seed,
            )
            self.assertEqual(
                (repo_prompt_dir / "swarm_proof.md").resolve(),
                loaded.effective.swarm.prompts.proof,
            )
            self.assertEqual(1, len(loaded.effective.validation_checks))
            self.assertEqual("unit", loaded.effective.validation_checks[0].name)
            self.assertEqual("pytest -q", loaded.effective.validation_checks[0].command)

    def test_inactive_provider_does_not_require_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            config_path = repo_dir / "config" / "config.toml"

            _write_prompt_tree(repo_dir / "config")
            _write(
                config_path,
                _user_config_text()
                + """

                [providers.openrouter]
                api_key_env = "OPENROUTER_API_KEY"
                base_url = "https://openrouter.ai/api/v1"
                allowed_models = ["gpt-5.4", "gpt-5.4-mini"]
                """,
            )

            loaded = load_effective_config(
                cwd=repo_dir,
                config_path=config_path,
                env={"OPENAI_API_KEY": "token"},
            )

            self.assertIn("openrouter", loaded.effective.providers)

    def test_active_provider_requires_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            config_path = repo_dir / "config" / "config.toml"

            _write_prompt_tree(repo_dir / "config")
            _write(config_path, _user_config_text())

            with self.assertRaises(ConfigError) as ctx:
                load_effective_config(
                    cwd=repo_dir,
                    config_path=config_path,
                    env={},
                )

            self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    def test_swarm_prompt_file_must_exist_when_swarm_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            config_path = repo_dir / "config" / "config.toml"

            _write_prompt_tree(repo_dir / "config")
            _write(
                config_path,
                _user_config_text().replace(
                    'danger_map = "prompts/swarm_danger_map.md"',
                    'danger_map = "prompts/missing-swarm.md"',
                ),
            )

            with self.assertRaises(ConfigError) as ctx:
                load_effective_config(
                    cwd=repo_dir,
                    config_path=config_path,
                    env={"OPENAI_API_KEY": "token"},
                )

            self.assertIn("Missing prompt file for swarm", str(ctx.exception))

    def test_legacy_swarm_schema_is_rejected_with_scaffold_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            config_path = repo_dir / "config" / "config.toml"

            _write_prompt_tree(repo_dir / "config")
            _write(
                config_path,
                _user_config_text().replace(
                    '[swarm.mode]\n        preset = "safe"',
                    '[swarm]\n        sweep_model = "gpt-5.4-mini"\n        proof_model = "gpt-5.4"\n        eligible_file_profile = "code_config_tests"\n        token_budget = 120000\n        allow_no_limit = true\n\n        [swarm.mode]\n        preset = "safe"',
                    1,
                ),
            )

            with self.assertRaises(ConfigError) as ctx:
                load_effective_config(
                    cwd=repo_dir,
                    config_path=config_path,
                    env={"OPENAI_API_KEY": "token"},
                )

            self.assertIn("Legacy swarm schema is no longer supported", str(ctx.exception))
            self.assertIn("awdit init-config", str(ctx.exception))

    def test_repo_dotenv_supplies_active_provider_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            config_path = repo_dir / "config" / "config.toml"

            _write_prompt_tree(repo_dir / "config")
            _write(config_path, _user_config_text())
            _write(default_repo_env_path(repo_dir), 'OPENAI_API_KEY="dotenv-token"')

            loaded = load_effective_config(
                cwd=repo_dir,
                config_path=config_path,
                env={},
            )

            self.assertEqual("dotenv-token", loaded.resolved_env["OPENAI_API_KEY"])

    def test_explicit_env_overrides_repo_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            config_path = repo_dir / "config" / "config.toml"

            _write_prompt_tree(repo_dir / "config")
            _write(config_path, _user_config_text())
            _write(default_repo_env_path(repo_dir), "OPENAI_API_KEY=dotenv-token")

            loaded = load_effective_config(
                cwd=repo_dir,
                config_path=config_path,
                env={"OPENAI_API_KEY": "shell-token"},
            )

            self.assertEqual("shell-token", loaded.resolved_env["OPENAI_API_KEY"])

    def test_missing_repo_config_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with self.assertRaises(ConfigError):
                load_effective_config(
                    cwd=root / "repo",
                    config_path=root / "repo" / "config" / "config.toml",
                    env={"OPENAI_API_KEY": "token"},
                )

    def test_invalid_default_model_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            config_path = repo_dir / "config" / "config.toml"
            _write_prompt_tree(repo_dir / "config")
            _write(
                config_path,
                _user_config_text().replace('default_model = "gpt-5.4"', 'default_model = "bad-model"', 1),
            )

            with self.assertRaises(ConfigError):
                load_effective_config(
                    cwd=repo_dir,
                    config_path=config_path,
                    env={"OPENAI_API_KEY": "token"},
                )

    def test_invalid_reasoning_effort_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            config_path = repo_dir / "config" / "config.toml"
            _write_prompt_tree(repo_dir / "config")
            _write(
                config_path,
                _user_config_text().replace('reasoning_effort = "medium"', 'reasoning_effort = "extreme"', 1),
            )

            with self.assertRaises(ConfigError):
                load_effective_config(
                    cwd=repo_dir,
                    config_path=config_path,
                    env={"OPENAI_API_KEY": "token"},
                )

    def test_swarm_reasoning_defaults_when_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            config_path = repo_dir / "config" / "config.toml"
            _write_prompt_tree(repo_dir / "config")
            _write(config_path, _user_config_text())

            loaded = load_effective_config(
                cwd=repo_dir,
                config_path=config_path,
                env={"OPENAI_API_KEY": "token"},
            )

            self.assertEqual("high", loaded.effective.swarm.reasoning.danger_map)
            self.assertEqual("low", loaded.effective.swarm.reasoning.seed)
            self.assertEqual("medium", loaded.effective.swarm.reasoning.proof)
            self.assertEqual("safe", loaded.effective.swarm.preset)
            self.assertEqual(2, loaded.effective.swarm.seed_max_parallel)
            self.assertEqual(1, loaded.effective.swarm.proof_max_parallel)
            self.assertEqual(3, loaded.effective.swarm.rate_limit_max_retries)

    def test_swarm_accepts_pr_changed_files_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            config_path = repo_dir / "config" / "config.toml"
            _write_prompt_tree(repo_dir / "config")
            _write(
                config_path,
                _user_config_text().replace(
                    'profile = "code_config_tests"',
                    'profile = "pr_changed_files"',
                    1,
                ),
            )

            loaded = load_effective_config(
                cwd=repo_dir,
                config_path=config_path,
                env={"OPENAI_API_KEY": "token"},
            )

            self.assertEqual("pr_changed_files", loaded.effective.swarm.eligible_file_profile)

    def test_swarm_rejects_all_tracked_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            config_path = repo_dir / "config" / "config.toml"
            _write_prompt_tree(repo_dir / "config")
            _write(
                config_path,
                _user_config_text().replace(
                    'profile = "code_config_tests"',
                    'profile = "all_tracked"',
                    1,
                ),
            )

            with self.assertRaises(ConfigError) as ctx:
                load_effective_config(
                    cwd=repo_dir,
                    config_path=config_path,
                    env={"OPENAI_API_KEY": "token"},
                )

            self.assertIn("swarm.files.profile", str(ctx.exception))

    def test_swarm_reasoning_partial_override_uses_defaults_for_missing_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            config_path = repo_dir / "config" / "config.toml"
            _write_prompt_tree(repo_dir / "config")
            _write(
                config_path,
                _user_config_text().replace(
                    '[swarm.prompts]',
                    '[swarm.reasoning]\nseed = "high"\n\n[swarm.prompts]',
                    1,
                ),
            )

            loaded = load_effective_config(
                cwd=repo_dir,
                config_path=config_path,
                env={"OPENAI_API_KEY": "token"},
            )

            self.assertEqual("high", loaded.effective.swarm.reasoning.danger_map)
            self.assertEqual("high", loaded.effective.swarm.reasoning.seed)
            self.assertEqual("medium", loaded.effective.swarm.reasoning.proof)

    def test_invalid_swarm_reasoning_effort_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            config_path = repo_dir / "config" / "config.toml"
            _write_prompt_tree(repo_dir / "config")
            _write(
                config_path,
                _user_config_text().replace(
                    '[swarm.prompts]',
                    '[swarm.reasoning]\nproof = "extreme"\n\n[swarm.prompts]',
                    1,
                ),
            )

            with self.assertRaises(ConfigError):
                load_effective_config(
                    cwd=repo_dir,
                    config_path=config_path,
                    env={"OPENAI_API_KEY": "token"},
                )

    def test_missing_prompt_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            config_path = repo_dir / "config" / "config.toml"
            _write_prompt_tree(repo_dir / "config")
            (repo_dir / "config" / "prompts" / "hunter_1.md").unlink()
            _write(config_path, _user_config_text())

            with self.assertRaises(ConfigError):
                load_effective_config(
                    cwd=repo_dir,
                    config_path=config_path,
                    env={"OPENAI_API_KEY": "token"},
                )

    def test_runtime_override_sources_and_save_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            repo_config = repo_dir / "config" / "config.toml"

            _write_prompt_tree(repo_dir / "config")
            _write(repo_config, _user_config_text())

            loaded = load_effective_config(
                cwd=repo_dir,
                config_path=repo_config,
                env={"OPENAI_API_KEY": "token"},
            )
            overridden = apply_runtime_overrides_with_env(
                loaded,
                {
                    "slots": {"solver_2": {"default_model": "gpt-5.4", "reasoning_effort": "high"}},
                    "swarm": {"reasoning": {"seed": "high"}},
                    "validation": {
                        "checks": [
                            {
                                "name": "lint",
                                "command": "ruff check .",
                                "timeout_seconds": 30,
                            }
                        ]
                    },
                    "resources": {
                        "shared": {
                            "include": ["https://example.com/runtime"],
                            "exclude": ["scratch/**"],
                        }
                    },
                },
                env={"OPENAI_API_KEY": "token"},
            )

            self.assertEqual(
                "runtime override",
                overridden.source_label("slots", "solver_2", "default_model"),
            )
            self.assertEqual("high", overridden.effective.slots["solver_2"].reasoning_effort)
            self.assertEqual(
                "runtime override",
                overridden.source_label("swarm", "reasoning", "seed"),
            )
            self.assertEqual("high", overridden.effective.swarm.reasoning.seed)
            self.assertEqual(
                ("https://example.com/runtime",),
                overridden.effective.resources.shared.include,
            )
            self.assertEqual(("scratch/**",), overridden.effective.resources.shared.exclude)
            save_repo_overrides(
                repo_config,
                {
                    "slots": {"solver_2": {"default_model": "gpt-5.4", "reasoning_effort": "high"}},
                    "swarm": {"reasoning": {"seed": "high"}},
                    "validation": {
                        "checks": [
                            {
                                "name": "lint",
                                "command": "ruff check .",
                                "timeout_seconds": 30,
                            }
                        ]
                    },
                    "resources": {
                        "shared": {
                            "include": ["https://example.com/runtime"],
                            "exclude": ["scratch/**"],
                        }
                    },
                },
            )

            saved = repo_config.read_text(encoding="utf-8")
            self.assertIn("[slots.solver_2]", saved)
            self.assertIn('default_model = "gpt-5.4"', saved)
            self.assertIn('reasoning_effort = "high"', saved)
            self.assertIn("[swarm.reasoning]", saved)
            self.assertIn('seed = "high"', saved)
            self.assertIn("[[validation.checks]]", saved)
            self.assertIn("[resources.shared]", saved)
            self.assertIn('include = ["https://example.com/runtime"]', saved)
            self.assertIn('exclude = ["scratch/**"]', saved)

    def test_resource_folder_defaults_are_auto_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            repo_config = repo_dir / "config" / "config.toml"

            _write_prompt_tree(repo_dir / "config")
            _write(repo_config, _user_config_text())

            shared_dir = default_shared_resources_path(repo_dir)
            slot_dir = default_slot_resources_path("hunter_1", repo_dir)
            _write(shared_dir / "refund-boundaries.md", "shared note")
            _write(shared_dir / "drafts" / "ignored.md", "ignore me")
            _write(shared_dir / ".gitkeep", "")
            _write(slot_dir / "auth-review-notes.md", "slot note")
            _write(slot_dir / "archive" / "ignored.md", "ignore me")

            loaded = load_effective_config(
                cwd=repo_dir,
                config_path=repo_config,
                env={"OPENAI_API_KEY": "token"},
            )

            self.assertEqual(
                ((shared_dir / "refund-boundaries.md").resolve(),),
                discover_resource_files(shared_dir, exclude=loaded.effective.resources.shared.exclude),
            )
            self.assertEqual(
                (str((shared_dir / "refund-boundaries.md").resolve()),),
                resolve_resource_section_items(shared_dir, loaded.effective.resources.shared),
            )
            self.assertEqual(
                (str((slot_dir / "auth-review-notes.md").resolve()),),
                resolve_resource_section_items(
                    slot_dir,
                    loaded.effective.resources.slots["hunter_1"],
                ),
            )

    def test_resource_auto_discovery_ignores_symlinked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            repo_config = repo_dir / "config" / "config.toml"

            _write_prompt_tree(repo_dir / "config")
            _write(repo_config, _user_config_text())

            shared_dir = default_shared_resources_path(repo_dir)
            outside_file = root / "outside" / "secret.md"
            _write(outside_file, "secret")
            _write(shared_dir / "kept.md", "kept")
            (shared_dir / "linked.md").symlink_to(outside_file)

            loaded = load_effective_config(
                cwd=repo_dir,
                config_path=repo_config,
                env={"OPENAI_API_KEY": "token"},
            )

            self.assertEqual(
                ((shared_dir / "kept.md").resolve(),),
                discover_resource_files(shared_dir, exclude=loaded.effective.resources.shared.exclude),
            )

    def test_repo_identity_prefers_remote_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            repo_dir.mkdir(parents=True)

            result = mock.Mock(returncode=0, stdout="https://example.com/acme/repo.git\n")
            with mock.patch("repo_memory.subprocess.run", return_value=result):
                identity = resolve_repo_identity(repo_dir)

            self.assertEqual("git_remote", identity.source_kind)
            self.assertEqual("https://example.com/acme/repo.git", identity.source_value)
            self.assertTrue(identity.repo_key.startswith("repo_"))
            self.assertEqual(37, len(identity.repo_key))

    def test_repo_identity_falls_back_to_repo_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            repo_dir.mkdir(parents=True)

            result = mock.Mock(returncode=1, stdout="")
            with mock.patch("repo_memory.subprocess.run", return_value=result):
                identity = resolve_repo_identity(repo_dir)

            self.assertEqual("repo_path", identity.source_kind)
            self.assertEqual(str(repo_dir.resolve()), identity.source_value)

    def test_repo_identity_migrates_legacy_repo_memory_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            repo_dir.mkdir(parents=True)

            result = mock.Mock(returncode=0, stdout="https://example.com/acme/repo.git\n")
            with mock.patch("repo_memory.subprocess.run", return_value=result):
                identity = resolve_repo_identity(repo_dir)

            legacy_dir = repo_dir / "repos" / legacy_repo_key(identity)
            _write(legacy_dir / "danger_map.md", "legacy map")
            _write(legacy_dir / "memory" / "repo_comments.md", "legacy comments")

            migrate_legacy_repo_memory_dir(repo_dir, identity)

            current_dir = repo_dir / "repos" / identity.repo_key
            self.assertTrue((current_dir / "danger_map.md").exists())
            self.assertTrue((current_dir / "memory" / "repo_comments.md").exists())
            self.assertFalse(legacy_dir.exists())

    def test_state_db_tracks_run_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            repo_dir.mkdir(parents=True)

            db_path = ensure_state_db(repo_dir)
            insert_run(
                cwd=repo_dir,
                run_id="2026-04-06_120000",
                repo_key="repo_deadbeef",
                mode="swarm",
                status="starting",
                run_dir=repo_dir / "runs" / "2026-04-06_120000",
            )
            update_run_status(
                cwd=repo_dir,
                run_id="2026-04-06_120000",
                status="completed",
                completed=True,
            )

            with sqlite3.connect(db_path) as connection:
                row = connection.execute(
                    "SELECT repo_key, mode, status, completed_at FROM runs WHERE run_id = ?",
                    ("2026-04-06_120000",),
                ).fetchone()

            self.assertEqual(("repo_deadbeef", "swarm", "completed"), row[:3])
            self.assertIsNotNone(row[3])

    def test_state_db_persists_failure_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            repo_dir.mkdir(parents=True)

            db_path = ensure_state_db(repo_dir)
            insert_run(
                cwd=repo_dir,
                run_id="2026-04-06_120000",
                repo_key="repo_deadbeef",
                mode="swarm",
                status="starting",
                run_dir=repo_dir / "runs" / "2026-04-06_120000",
            )
            record_run_failure(
                cwd=repo_dir,
                run_id="2026-04-06_120000",
                failure_stage="seed",
                failure_worker_id="SEED-014",
                failure_message="Structured swarm response missing keys: notes",
                failure_artifact=repo_dir / "runs" / "2026-04-06_120000" / "swarm" / "failure_diagnostic.json",
            )

            with sqlite3.connect(db_path) as connection:
                row = connection.execute(
                    """
                    SELECT failure_stage, failure_worker_id, failure_message, failure_artifact
                    FROM runs
                    WHERE run_id = ?
                    """,
                    ("2026-04-06_120000",),
                ).fetchone()

            self.assertEqual("seed", row[0])
            self.assertEqual("SEED-014", row[1])
            self.assertIn("missing keys: notes", row[2])
            self.assertTrue(row[3].endswith("failure_diagnostic.json"))

    def test_state_db_persists_learned_model_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            repo_dir.mkdir(parents=True)

            save_learned_model_limit(
                cwd=repo_dir,
                provider="openai",
                model="gpt-5.4-mini",
                learned_tpm_limit=200000,
                headroom_fraction=0.85,
                observed_peak_input_tokens={"seed_file": 40, "proof_issue": 25},
            )

            record = load_learned_model_limit(
                cwd=repo_dir,
                provider="openai",
                model="gpt-5.4-mini",
            )

            self.assertIsNotNone(record)
            self.assertEqual(200000, record.learned_tpm_limit)
            self.assertEqual(0.85, record.headroom_fraction)
            self.assertEqual({"seed_file": 40, "proof_issue": 25}, record.observed_peak_input_tokens)


if __name__ == "__main__":
    unittest.main()

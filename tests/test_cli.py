from __future__ import annotations

import io
import json
import os
import sqlite3
import time
import textwrap
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cli import _allocate_run_dir, main
from config import SLOT_NAMES, load_effective_config
from paths import repos_root, runs_root, state_root
from provider_openai import BackgroundPollResult, ProviderBackgroundHandle, ProviderTurnResult
from repo_memory import RepoIdentity


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def _write_prompt_tree(base: Path) -> None:
    prompt_dir = base / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    for slot in SLOT_NAMES:
        (prompt_dir / f"{slot}.md").write_text(f"# {slot}\n", encoding="utf-8")
    (prompt_dir / "swarm_danger_map.md").write_text("# swarm danger map\n", encoding="utf-8")
    (prompt_dir / "swarm_seed.md").write_text("# swarm seed\n", encoding="utf-8")
    (prompt_dir / "swarm_proof.md").write_text("# swarm proof\n", encoding="utf-8")


def _user_config_text() -> str:
    slot_blocks = []
    for slot in SLOT_NAMES:
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

        [repo_memory]
        enabled = true
        require_danger_map_approval = true
        confirm_refresh_on_startup = true
        auto_update_on_completion = true

        [resources.shared]
        include = ["https://example.com/shared-reference"]
        exclude = ["ignored/**"]

        [resources.slots.hunter_1]
        include = ["manual/hunter-note.md"]
        exclude = []

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


class BackgroundSequenceProvider:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self._pending_payloads = list(payloads)
        self._responses: dict[str, dict[str, object]] = {}
        self.start_calls: list[dict[str, object]] = []

    def start_background_turn(self, **kwargs):
        if not self._pending_payloads:
            raise AssertionError("No more background payloads prepared for test provider.")
        response_id = f"bg_{len(self.start_calls) + 1}"
        payload = self._pending_payloads.pop(0)
        self._responses[response_id] = payload
        self.start_calls.append(kwargs)
        return ProviderBackgroundHandle(response_id=response_id)

    def poll_background_turn(self, **kwargs):
        response_id = kwargs["handle"].response_id
        payload = self._responses.pop(response_id)
        return BackgroundPollResult(
            status="completed",
            response_id=response_id,
            final_text=json.dumps(payload),
            tool_traces=(),
        )

    def cancel_background_turn(self, handle):
        self._responses.pop(handle.response_id, None)
        return "cancelled"

    def classify_provider_failure(self, value):
        return None


def _assert_no_triple_newlines(testcase: unittest.TestCase, text: str) -> None:
    testcase.assertNotIn("\n\n\n", text)


def _set_awdit_data_root(testcase: unittest.TestCase) -> Path:
    data_root_dir = tempfile.TemporaryDirectory()
    testcase.addCleanup(data_root_dir.cleanup)
    data_root = Path(data_root_dir.name) / "awdit-data"
    env_patcher = mock.patch.dict(os.environ, {"AWDIT_DATA_ROOT": str(data_root)})
    env_patcher.start()
    testcase.addCleanup(env_patcher.stop)
    return data_root


class HelpFormattingTests(unittest.TestCase):
    def _capture_help(self, argv: list[str]) -> str:
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            with self.assertRaises(SystemExit) as exc_info:
                main(argv)
        self.assertEqual(0, exc_info.exception.code)
        return stdout.getvalue()

    def test_root_help_uses_moderate_spacing(self) -> None:
        output = self._capture_help(["--help"])

        self.assertIn(
            "usage: awdit [-h] {review,swarm,init-config,list-models} ...\n\npositional arguments:",
            output,
        )
        self.assertIn("\n\noptions:\n", output)
        _assert_no_triple_newlines(self, output)

    def test_swarm_help_uses_moderate_spacing(self) -> None:
        output = self._capture_help(["swarm", "--help"])

        self.assertIn("usage: awdit swarm [-h] [--config CONFIG] [--env-file ENV_FILE]", output)
        self.assertIn("[--base-ref BASE_REF]\n\noptions:\n", output)
        _assert_no_triple_newlines(self, output)


class ReviewCliTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.data_root = _set_awdit_data_root(self)

    def _input_mock(self, inputs: list[str], stdout: io.StringIO):
        remaining = list(inputs)

        def _fake_input(prompt: str = "") -> str:
            stdout.write(prompt)
            if not remaining:
                if prompt.startswith("runtime> "):
                    time.sleep(0.02)
                    return "quit"
                raise AssertionError("No more test inputs available.")
            if prompt.startswith("runtime> "):
                time.sleep(0.02)
            return remaining.pop(0)

        return _fake_input

    def _loaded_config(self, repo_dir: Path):
        config_dir = repo_dir / "config"
        config_path = config_dir / "config.toml"

        _write_prompt_tree(config_dir)
        _write(config_path, _user_config_text())
        _write(repo_dir / "config" / "manual" / "hunter-note.md", "manual hunter note")
        return load_effective_config(
            cwd=repo_dir,
            config_path=config_path,
            env={"OPENAI_API_KEY": "token"},
        )

    def _loaded_config_from_text(self, repo_dir: Path, config_text: str):
        config_dir = repo_dir / "config"
        config_path = config_dir / "config.toml"

        _write_prompt_tree(config_dir)
        _write(config_path, config_text)
        _write(repo_dir / "config" / "manual" / "hunter-note.md", "manual hunter note")
        return load_effective_config(
            cwd=repo_dir,
            config_path=config_path,
            env={"OPENAI_API_KEY": "token"},
        )

    def _loaded_config_from_text(self, repo_dir: Path, config_text: str):
        config_dir = repo_dir / "config"
        config_path = config_dir / "config.toml"

        _write_prompt_tree(config_dir)
        _write(config_path, config_text)
        return load_effective_config(
            cwd=repo_dir,
            config_path=config_path,
            env={"OPENAI_API_KEY": "token"},
        )

    def _run_review(self, repo_dir: Path, loaded, inputs: list[str]) -> tuple[int, str]:
        stdout = io.StringIO()
        inputs = [*inputs, "n"]
        with (
            mock.patch("cli.Path.cwd", return_value=repo_dir),
            mock.patch("cli.load_effective_config", return_value=loaded),
            mock.patch("cli._make_run_id", return_value="2026-03-29_101530"),
            mock.patch("builtins.input", side_effect=self._input_mock(inputs, stdout)),
            mock.patch("sys.stdout", stdout),
        ):
            result = main(["review"])
        return result, stdout.getvalue()

    def test_review_offers_runtime_prompt_after_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "config" / "resources" / "shared" / "refund-boundaries.md", "shared")
            loaded = self._loaded_config(repo_dir)

            result, output = self._run_review(repo_dir, loaded, ["n", "y", "n"])

            self.assertEqual(0, result)
            self.assertIn("Run-scoped resource snapshot", output)
            self.assertIn("Enter one-slot runtime prototype mode?", output)
            self.assertLess(output.index("Run-scoped resource snapshot"), output.index("Enter one-slot runtime prototype mode?"))

    def test_review_runtime_receives_resolved_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "config" / "resources" / "shared" / "refund-boundaries.md", "shared")
            loaded = self._loaded_config(repo_dir)
            captured_kwargs: dict[str, object] = {}

            class FakeRuntime:
                def __init__(self, **kwargs):
                    captured_kwargs.update(kwargs)

                def interactive_loop(self) -> int:
                    return 0

            with mock.patch("cli.OneSlotRuntime", FakeRuntime):
                result, output = self._run_review(repo_dir, loaded, ["n", "y", "n", "y"])

            self.assertEqual(0, result)
            self.assertIn("Prototype runtime setup", output)
            self.assertEqual(self.data_root.resolve(), captured_kwargs["data_root"])
            self.assertEqual(runs_root(repo_dir) / "2026-03-29_101530", captured_kwargs["run_dir"])

    def test_review_output_uses_moderate_spacing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "config" / "resources" / "shared" / "refund-boundaries.md", "shared")
            loaded = self._loaded_config(repo_dir)

            result, output = self._run_review(repo_dir, loaded, ["n", "y", "n"])

            self.assertEqual(0, result)
            self.assertIn("Resource defaults\n\nNote for user:", output)
            self.assertIn("Final effective config\n\nEffective config summary", output)
            self.assertIn("Run-scoped resource snapshot\n- Run id:", output)
            self.assertIn("Resource summary:", output)
            self.assertIn("\n\nResources selected for this run\n- Shared resources for this run:", output)
            _assert_no_triple_newlines(self, output)

    def test_review_can_enter_runtime_and_run_foreground_dispatch(self) -> None:
        class FakeProvider:
            def start_foreground_turn(self, **kwargs):
                return ProviderTurnResult(
                    response_id="resp_1",
                    final_text="foreground result",
                    tool_traces=(),
                    status="completed",
                    model=kwargs["model"],
                )

            def start_background_turn(self, **kwargs):
                return ProviderBackgroundHandle(response_id="bg_1")

            def poll_background_turn(self, **kwargs):
                return BackgroundPollResult(
                    status="completed",
                    response_id="bg_1",
                    final_text="background result",
                    tool_traces=(),
                )

            def classify_provider_failure(self, value):
                return None

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "config" / "resources" / "shared" / "refund-boundaries.md", "shared")
            loaded = self._loaded_config(repo_dir)

            stdout = io.StringIO()
            with (
                mock.patch("cli.Path.cwd", return_value=repo_dir),
                mock.patch("cli.load_effective_config", return_value=loaded),
                mock.patch("cli._make_run_id", return_value="2026-03-29_101530"),
                mock.patch(
                    "runtime.OpenAIResponsesProvider.from_loaded_config",
                    return_value=FakeProvider(),
                ),
                mock.patch(
                    "builtins.input",
                    side_effect=self._input_mock(
                        [
                            "n",
                            "y",
                            "n",
                            "y",
                            "dispatch-fg",
                            "quit",
                        ],
                        stdout,
                    ),
                ),
                mock.patch("sys.stdout", stdout),
            ):
                result = main(["review"])

            self.assertEqual(0, result)
            run_dir = runs_root(repo_dir) / "2026-03-29_101530"
            artifacts_dir = run_dir / "session_state" / "artifacts" / "hunter_1"
            response_files = list(artifacts_dir.glob("*/response.txt"))
            self.assertTrue(response_files)
            output = stdout.getvalue()
            self.assertIn("One-slot runtime prototype mode", output)
            self.assertIn("Dispatch commands: dispatch-fg, dispatch-bg", output)
            transcript_path = run_dir / "logs" / "prototype__2026-03-29_101530.txt"
            self.assertTrue(transcript_path.exists())
            transcript = transcript_path.read_text(encoding="utf-8")
            self.assertIn("Prototype runtime setup", transcript)
            self.assertIn("Prototype transcript:", transcript)
            self.assertIn("runtime> dispatch-fg", transcript)
            self.assertIn("Dispatch summary", transcript)
            self.assertIn("- mode: foreground", transcript)
            self.assertIn("- label: Hunter 1 foreground run", transcript)
            self.assertIn("- key: hunter_1/foreground", transcript)
            self.assertNotIn("Work label:", transcript)
            self.assertNotIn("Work key:", transcript)
            self.assertNotIn("Instructions source", transcript)
            self.assertIn("quit", transcript)
            self.assertIn(f"Foreground dispatch {response_files[0].parent.name} finished with status=completed.", transcript)
            self.assertNotIn("Choose the default dispatch mode", transcript)

    def test_review_captures_streamed_foreground_output_in_transcript(self) -> None:
        class StreamingProvider:
            def start_foreground_turn(self, **kwargs):
                event_callback = kwargs["event_callback"]
                event_callback("output_delta", {"delta": "streamed "})
                event_callback("output_delta", {"delta": "reply"})
                return ProviderTurnResult(
                    response_id="resp_stream",
                    final_text="streamed reply",
                    tool_traces=(),
                    status="completed",
                    model=kwargs["model"],
                )

            def start_background_turn(self, **kwargs):
                raise AssertionError("background should not be used")

            def poll_background_turn(self, **kwargs):
                raise AssertionError("background should not be used")

            def classify_provider_failure(self, value):
                return None

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "config" / "resources" / "shared" / "refund-boundaries.md", "shared")
            loaded = self._loaded_config(repo_dir)

            stdout = io.StringIO()
            with (
                mock.patch("cli.Path.cwd", return_value=repo_dir),
                mock.patch("cli.load_effective_config", return_value=loaded),
                mock.patch("cli._make_run_id", return_value="2026-03-29_101530"),
                mock.patch(
                    "runtime.OpenAIResponsesProvider.from_loaded_config",
                    return_value=StreamingProvider(),
                ),
                mock.patch(
                    "builtins.input",
                    side_effect=self._input_mock(
                        [
                            "n",
                            "y",
                            "n",
                            "y",
                            "dispatch-fg",
                            "quit",
                        ],
                        stdout,
                    ),
                ),
                mock.patch("sys.stdout", stdout),
            ):
                result = main(["review"])

            self.assertEqual(0, result)
            transcript_path = runs_root(repo_dir) / "2026-03-29_101530" / "logs" / "prototype__2026-03-29_101530.txt"
            transcript = transcript_path.read_text(encoding="utf-8")
            self.assertIn("streamed reply", transcript)
            self.assertIn("Foreground dispatch dispatch_", transcript)
            _assert_no_triple_newlines(self, transcript)

    def test_review_can_run_background_dispatch_and_keep_status_available(self) -> None:
        class FakeBackgroundProvider:
            def __init__(self) -> None:
                self.poll_calls = 0

            def start_foreground_turn(self, **kwargs):
                raise AssertionError("foreground should not be used")

            def start_background_turn(self, **kwargs):
                return ProviderBackgroundHandle(response_id="bg_1")

            def poll_background_turn(self, **kwargs):
                self.poll_calls += 1
                if self.poll_calls == 1:
                    return BackgroundPollResult(
                        status="running",
                        response_id="bg_1",
                        final_text="",
                        tool_traces=(),
                    )
                return BackgroundPollResult(
                    status="completed",
                    response_id="bg_2",
                    final_text="background result",
                    tool_traces=(),
                )

            def classify_provider_failure(self, value):
                return None

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "config" / "resources" / "shared" / "refund-boundaries.md", "shared")
            loaded = self._loaded_config(repo_dir)
            provider = FakeBackgroundProvider()
            stdout = io.StringIO()
            with (
                mock.patch("cli.Path.cwd", return_value=repo_dir),
                mock.patch("cli.load_effective_config", return_value=loaded),
                mock.patch("cli._make_run_id", return_value="2026-03-29_101530"),
                mock.patch(
                    "runtime.OpenAIResponsesProvider.from_loaded_config",
                    return_value=provider,
                ),
                mock.patch(
                    "builtins.input",
                    side_effect=self._input_mock(
                        [
                            "n",
                            "y",
                            "n",
                            "y",
                            "dispatch-bg",
                            "status",
                            "events",
                            "status",
                            "events",
                            "quit",
                            "quit",
                            "quit",
                        ],
                        stdout,
                    ),
                ),
                mock.patch("sys.stdout", stdout),
            ):
                result = main(["review"])

            self.assertEqual(0, result)
            self.assertIn("Runtime status", stdout.getvalue())
            self.assertIn("Recent events", stdout.getvalue())
            self.assertIn("dispatch-bg", stdout.getvalue())
            _assert_no_triple_newlines(self, stdout.getvalue())

    def test_list_models_prints_available_openai_models(self) -> None:
        class FakeProvider:
            def list_model_ids(self):
                return ("gpt-5.4", "gpt-5.4-mini")

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            loaded = self._loaded_config(repo_dir)
            stdout = io.StringIO()
            with (
                mock.patch("cli.Path.cwd", return_value=repo_dir),
                mock.patch("cli.load_effective_config", return_value=loaded),
                mock.patch(
                    "cli.OpenAIResponsesProvider.from_loaded_config",
                    return_value=FakeProvider(),
                ),
                mock.patch("sys.stdout", stdout),
            ):
                result = main(["list-models"])

            self.assertEqual(0, result)
            output = stdout.getvalue()
            self.assertIn("Available openai models", output)
            self.assertIn("gpt-5.4", output)
            self.assertIn("gpt-5.4-mini", output)

    def test_review_accepts_defaults_and_writes_run_scoped_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "config" / "resources" / "shared" / "refund-boundaries.md", "shared")
            _write(
                repo_dir / "config" / "resources" / "shared" / "ignored" / "draft.md",
                "ignored",
            )
            _write(
                repo_dir / "config" / "resources" / "slots" / "hunter_1" / "auth-review-notes.md",
                "slot",
            )
            loaded = self._loaded_config(repo_dir)

            result, output = self._run_review(repo_dir, loaded, ["n", "y", "n"])

            self.assertEqual(0, result)
            self.assertIn("Shared resources for this run", output)
            self.assertIn("Note for user:", output)
            self.assertIn("Everything under config/resources/shared/", output)
            run_dir = runs_root(repo_dir) / "2026-03-29_101530"
            shared_manifest = run_dir / "resources" / "shared" / "manifest.md"
            slot_manifest = run_dir / "resources" / "slots" / "hunter_1" / "manifest.md"
            summary_path = run_dir / "resources" / "summary.md"

            self.assertTrue(shared_manifest.exists())
            self.assertTrue(slot_manifest.exists())
            self.assertTrue(summary_path.exists())
            self.assertFalse((run_dir / "logs").exists())

            shared_text = shared_manifest.read_text(encoding="utf-8")
            self.assertIn("refund-boundaries.md", shared_text)
            self.assertNotIn("ignored/draft.md", shared_text)
            self.assertIn("https://example.com/shared-reference", shared_text)
            self.assertIn("(not fetched)", shared_text)
            self.assertTrue(
                (run_dir / "resources" / "shared" / "staged" / "01_refund-boundaries.md").exists()
            )

            slot_text = slot_manifest.read_text(encoding="utf-8")
            self.assertIn("auth-review-notes.md", slot_text)
            self.assertIn("hunter-note.md", slot_text)
            self.assertTrue(
                (run_dir / "resources" / "slots" / "hunter_1" / "staged" / "01_auth-review-notes.md").exists()
            )
            self.assertTrue(
                (run_dir / "resources" / "slots" / "hunter_1" / "staged" / "02_hunter-note.md").exists()
            )

    def test_awdit_self_review_stages_design_docs_and_prompt_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "awdit"
            _write(repo_dir / "docs" / "architecture.md", "architecture")
            _write(repo_dir / "docs" / "agent-isolation-workflow.md", "workflow")
            _write(repo_dir / "docs" / "e2e-cli-walkthrough.txt", "walkthrough")
            _write(
                repo_dir / "docs" / "PROPOSED_FILE_STRUCTURE_CONFIG_BEHAVIOUR.txt",
                "file structure",
            )
            _write(repo_dir / "config" / "resources" / "shared" / "architecture.md", "architecture")
            _write(
                repo_dir / "config" / "resources" / "shared" / "agent-isolation-workflow.md",
                "workflow",
            )
            loaded = self._loaded_config(repo_dir)

            result, output = self._run_review(repo_dir, loaded, ["n", "y", "n"])

            self.assertEqual(0, result)
            self.assertIn("Prompt snapshots", output)
            run_dir = runs_root(repo_dir) / "2026-03-29_101530"
            shared_manifest = run_dir / "resources" / "shared" / "manifest.md"
            run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

            shared_text = shared_manifest.read_text(encoding="utf-8")
            self.assertIn("architecture.md", shared_text)
            self.assertIn("agent-isolation-workflow.md", shared_text)
            self.assertTrue((run_dir / "prompts" / "hunter_1.md").exists())
            self.assertIn("prompt_snapshot", run_json["slots"]["hunter_1"])

    def test_review_blocks_missing_local_shared_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            loaded = self._loaded_config_from_text(
                repo_dir,
                _user_config_text().replace(
                    'include = ["https://example.com/shared-reference"]',
                    'include = ["missing/shared-note.md"]',
                ),
            )

            result, output = self._run_review(repo_dir, loaded, ["n", "y", "n"])

            self.assertEqual(0, result)
            self.assertIn("Cannot continue with missing local resources:", output)
            self.assertIn("Review canceled before launch.", output)
            self.assertFalse((runs_root(repo_dir) / "2026-03-29_101530").exists())

    def test_review_edit_replaces_effective_lists_for_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "config" / "resources" / "shared" / "refund-boundaries.md", "shared")
            _write(
                repo_dir / "config" / "resources" / "slots" / "hunter_1" / "auth-review-notes.md",
                "slot",
            )
            override_shared = repo_dir / "notes" / "payment-threat-model.md"
            override_slot_file = repo_dir / "notes" / "auth-extra.md"
            override_slot_dir = repo_dir / "notes" / "auth-docs"
            _write(override_shared, "manual shared")
            _write(override_slot_file, "manual slot")
            _write(override_slot_dir / "overview.md", "folder note")
            loaded = self._loaded_config(repo_dir)

            result, _ = self._run_review(
                repo_dir,
                loaded,
                [
                    "n",
                    "e",
                    f"{override_shared}, https://example.com/manual",
                    "y",
                    "1",
                    "e",
                    f"{override_slot_file}, {override_slot_dir}",
                    "9",
                ],
            )

            self.assertEqual(0, result)
            run_dir = runs_root(repo_dir) / "2026-03-29_101530"
            shared_manifest = run_dir / "resources" / "shared" / "manifest.md"
            slot_manifest = run_dir / "resources" / "slots" / "hunter_1" / "manifest.md"

            shared_text = shared_manifest.read_text(encoding="utf-8")
            self.assertIn("payment-threat-model.md", shared_text)
            self.assertIn("https://example.com/manual", shared_text)
            self.assertNotIn("refund-boundaries.md", shared_text)

            slot_text = slot_manifest.read_text(encoding="utf-8")
            self.assertIn("auth-extra.md", slot_text)
            self.assertIn("auth-docs", slot_text)
            self.assertNotIn("auth-review-notes.md", slot_text)
            self.assertTrue(
                (run_dir / "resources" / "slots" / "hunter_1" / "staged" / "02_auth-docs" / "overview.md").exists()
            )

    def test_review_exit_stops_before_creating_run_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "config" / "resources" / "shared" / "refund-boundaries.md", "shared")
            loaded = self._loaded_config(repo_dir)

            result, output = self._run_review(repo_dir, loaded, ["n", "n"])

            self.assertEqual(0, result)
            self.assertIn("Review canceled before launch.", output)
            self.assertFalse((runs_root(repo_dir) / "2026-03-29_101530").exists())


class SwarmCliTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.data_root = _set_awdit_data_root(self)

    def _input_mock(self, inputs: list[str], stdout: io.StringIO):
        remaining = list(inputs)

        def _fake_input(prompt: str = "") -> str:
            stdout.write(prompt)
            if not remaining:
                raise AssertionError("No more test inputs available.")
            return remaining.pop(0)

        return _fake_input

    def _loaded_config(self, repo_dir: Path):
        config_dir = repo_dir / "config"
        config_path = config_dir / "config.toml"

        _write_prompt_tree(config_dir)
        _write(config_path, _user_config_text())
        _write(repo_dir / "config" / "manual" / "hunter-note.md", "manual hunter note")
        return load_effective_config(
            cwd=repo_dir,
            config_path=config_path,
            env={"OPENAI_API_KEY": "token"},
        )

    def _loaded_config_from_text(self, repo_dir: Path, config_text: str):
        config_dir = repo_dir / "config"
        config_path = config_dir / "config.toml"

        _write_prompt_tree(config_dir)
        _write(config_path, config_text)
        _write(repo_dir / "config" / "manual" / "hunter-note.md", "manual hunter note")
        return load_effective_config(
            cwd=repo_dir,
            config_path=config_path,
            env={"OPENAI_API_KEY": "token"},
        )

    def _run_swarm(
        self,
        repo_dir: Path,
        loaded,
        provider,
        inputs: list[str],
        *,
        argv: list[str] | None = None,
        run_id: str = "2026-04-06_121500",
    ) -> tuple[int, str]:
        stdout = io.StringIO()
        identity = RepoIdentity(
            repo_name=repo_dir.name,
            repo_key=f"{repo_dir.name}_deadbeef",
            source_kind="repo_path",
            source_value=str(repo_dir.resolve()),
            repo_dir=repo_dir.resolve(),
        )
        with (
            mock.patch("cli.Path.cwd", return_value=repo_dir),
            mock.patch("cli.load_effective_config", return_value=loaded),
            mock.patch("cli._make_run_id", return_value=run_id),
            mock.patch("cli.OpenAIResponsesProvider.from_loaded_config", return_value=provider),
            mock.patch("cli.resolve_repo_identity", return_value=identity),
            mock.patch("swarm.resolve_repo_identity", return_value=identity),
            mock.patch("builtins.input", side_effect=self._input_mock(inputs, stdout)),
            mock.patch("sys.stdout", stdout),
        ):
            result = main(argv or ["swarm"])
        return result, stdout.getvalue()

    def test_allocate_run_dir_retries_until_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            (runs_root(repo_dir) / "duplicate").mkdir(parents=True, exist_ok=True)

            with mock.patch("cli._make_run_id", side_effect=["duplicate", "duplicate", "unique"]):
                run_id, run_dir = _allocate_run_dir(repo_dir)

            self.assertEqual("unique", run_id)
            self.assertEqual(runs_root(repo_dir) / "unique", run_dir)
            self.assertTrue(run_dir.exists())

    def test_swarm_generates_and_accepts_new_danger_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            loaded = self._loaded_config(repo_dir)

            result, output = self._run_swarm(
                repo_dir,
                loaded,
                BackgroundSequenceProvider(
                    [
                        {
                            "trust_boundaries": ["api boundary"],
                            "risky_sinks": ["sql write path"],
                            "auth_assumptions": ["session cookie is trusted"],
                            "hot_paths": ["app/routes.py"],
                            "notes": ["watch org scoping"],
                        }
                    ]
                ),
                ["n", "y", "y", "y", "y"],
            )

            self.assertEqual(0, result)
            self.assertIn("Starting new swarm run...", output)
            self.assertIn("No repo danger map exists for this repository yet.", output)
            self.assertIn("Repo danger map ready:", output)
            self.assertIn("Swarm preflight", output)
            self.assertIn("Swarm startup preflight is ready.", output)
            self.assertIn("Swarm complete.", output)
            self.assertIn("Case groups:", output)

            repo_dir_root = repos_root(repo_dir) / "repo_deadbeef"
            self.assertTrue((repo_dir_root / "danger_map.md").exists())
            self.assertTrue((repo_dir_root / "danger_map.json").exists())
            self.assertTrue((repo_dir_root / "memory" / "repo_comments.md").exists())

            with sqlite3.connect(state_root(repo_dir) / "awdit.db") as connection:
                row = connection.execute(
                    "SELECT mode, status, completed_at FROM runs WHERE run_id = ?",
                    ("2026-04-06_121500",),
                ).fetchone()
            self.assertEqual(("swarm", "completed"), row[:2])
            self.assertIsNotNone(row[2])

    def test_swarm_rejects_base_ref_outside_pr_changed_files_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            loaded = self._loaded_config(repo_dir)
            stdout = io.StringIO()

            with (
                mock.patch("cli.Path.cwd", return_value=repo_dir),
                mock.patch("cli.load_effective_config", return_value=loaded),
                mock.patch("sys.stdout", stdout),
            ):
                result = main(["swarm", "--base-ref", "origin/main"])

            self.assertEqual(1, result)
            self.assertIn(
                '--base-ref is only valid when [swarm.files].profile = "pr_changed_files"',
                stdout.getvalue(),
            )

    def test_swarm_pr_changed_files_mode_defaults_base_ref_to_main(self) -> None:
        def _fake_git_run(command, **kwargs):
            if command[:2] == ["git", "diff"]:
                self.assertEqual("main...HEAD", command[-1])
                return mock.Mock(returncode=0, stdout="M\tapp/service.py\n", stderr="")
            if command[:2] == ["git", "ls-files"]:
                return mock.Mock(returncode=1, stdout="", stderr="not a git repo")
            raise AssertionError(f"Unexpected git command: {command}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            loaded = self._loaded_config_from_text(
                repo_dir,
                _user_config_text().replace('profile = "code_config_tests"', 'profile = "pr_changed_files"', 1),
            )

            with mock.patch("swarm.subprocess.run", side_effect=_fake_git_run):
                result, output = self._run_swarm(
                    repo_dir,
                    loaded,
                    BackgroundSequenceProvider(
                        [
                            {
                                "trust_boundaries": ["api boundary"],
                                "risky_sinks": ["sql write path"],
                                "auth_assumptions": ["session cookie is trusted"],
                                "hot_paths": ["app/service.py"],
                                "notes": ["watch org scoping"],
                            }
                        ]
                    ),
                    ["n", "y", "y", "n"],
                )

            self.assertEqual(0, result)
            self.assertIn("File handling mode: PR changed files", output)
            self.assertIn("Base ref: main", output)

            run_json = json.loads(
                ((runs_root(repo_dir) / "2026-04-06_121500") / "run.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("pr_changed_files", run_json["swarm"]["files"]["profile"])
            self.assertEqual("main", run_json["swarm"]["files"]["base_ref"])

    def test_swarm_pr_changed_files_mode_honors_base_ref_override(self) -> None:
        def _fake_git_run(command, **kwargs):
            if command[:2] == ["git", "diff"]:
                self.assertEqual("origin/main...HEAD", command[-1])
                return mock.Mock(returncode=0, stdout="M\tapp/service.py\n", stderr="")
            if command[:2] == ["git", "ls-files"]:
                return mock.Mock(returncode=1, stdout="", stderr="not a git repo")
            raise AssertionError(f"Unexpected git command: {command}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            loaded = self._loaded_config_from_text(
                repo_dir,
                _user_config_text().replace('profile = "code_config_tests"', 'profile = "pr_changed_files"', 1),
            )

            with mock.patch("swarm.subprocess.run", side_effect=_fake_git_run):
                result, output = self._run_swarm(
                    repo_dir,
                    loaded,
                    BackgroundSequenceProvider(
                        [
                            {
                                "trust_boundaries": ["api boundary"],
                                "risky_sinks": ["sql write path"],
                                "auth_assumptions": ["session cookie is trusted"],
                                "hot_paths": ["app/service.py"],
                                "notes": ["watch org scoping"],
                            }
                        ]
                    ),
                    ["n", "y", "y", "n"],
                    argv=["swarm", "--base-ref", "origin/main"],
                )

            self.assertEqual(0, result)
            self.assertIn("Base ref: origin/main", output)

            run_json = json.loads(
                ((runs_root(repo_dir) / "2026-04-06_121500") / "run.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("origin/main", run_json["swarm"]["files"]["base_ref"])

    def test_swarm_pr_changed_files_mode_exits_cleanly_when_no_files_remain(self) -> None:
        def _fake_git_run(command, **kwargs):
            if command[:2] == ["git", "diff"]:
                return mock.Mock(returncode=0, stdout="", stderr="")
            if command[:2] == ["git", "ls-files"]:
                return mock.Mock(returncode=1, stdout="", stderr="not a git repo")
            raise AssertionError(f"Unexpected git command: {command}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            loaded = self._loaded_config_from_text(
                repo_dir,
                _user_config_text().replace('profile = "code_config_tests"', 'profile = "pr_changed_files"', 1),
            )
            provider = BackgroundSequenceProvider(
                [
                    {
                        "trust_boundaries": ["api boundary"],
                        "risky_sinks": ["sql write path"],
                        "auth_assumptions": ["session cookie is trusted"],
                        "hot_paths": ["app/service.py"],
                        "notes": ["watch org scoping"],
                    }
                ]
            )

            with mock.patch("swarm.subprocess.run", side_effect=_fake_git_run):
                result, output = self._run_swarm(
                    repo_dir,
                    loaded,
                    provider,
                    ["n", "y", "y"],
                )

            self.assertEqual(0, result)
            self.assertIn("No processable changed files remain for swarm.", output)
            self.assertIn("Swarm finished without launching workers.", output)
            self.assertNotIn("Launch swarm?", output)
            self.assertEqual(1, len(provider.start_calls))

            run_json = json.loads(
                ((runs_root(repo_dir) / "2026-04-06_121500") / "run.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual([], run_json["eligible_files"])

            with sqlite3.connect(state_root(repo_dir) / "awdit.db") as connection:
                row = connection.execute(
                    "SELECT status FROM runs WHERE run_id = ?",
                    ("2026-04-06_121500",),
                ).fetchone()
            self.assertEqual(("completed",), row)

    def test_swarm_can_load_external_config_for_foreign_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "target-repo"
            external_dir = root / "external-config"
            config_path = external_dir / "swarm.toml"
            stdout = io.StringIO()
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            _write_prompt_tree(external_dir)
            _write(external_dir / "shared" / "foreign-reference.md", "foreign note")
            _write(
                config_path,
                _user_config_text().replace(
                    'include = ["https://example.com/shared-reference"]',
                    'include = ["shared/foreign-reference.md"]',
                    1,
                ),
            )
            identity = RepoIdentity(
                repo_name=repo_dir.name,
                repo_key=f"{repo_dir.name}_deadbeef",
                source_kind="repo_path",
                source_value=str(repo_dir.resolve()),
                repo_dir=repo_dir.resolve(),
            )

            with (
                mock.patch("cli.Path.cwd", return_value=repo_dir),
                mock.patch("cli._make_run_id", return_value="2026-04-06_121500"),
                mock.patch(
                    "cli.OpenAIResponsesProvider.from_loaded_config",
                    return_value=BackgroundSequenceProvider(
                        [
                            {
                                "trust_boundaries": ["api boundary"],
                                "risky_sinks": ["sql write path"],
                                "auth_assumptions": ["session cookie is trusted"],
                                "hot_paths": ["app/service.py"],
                                "notes": ["watch org scoping"],
                            }
                        ]
                    ),
                ),
                mock.patch("cli.resolve_repo_identity", return_value=identity),
                mock.patch("swarm.resolve_repo_identity", return_value=identity),
                mock.patch("builtins.input", side_effect=self._input_mock(["n", "y", "y", "n"], stdout)),
                mock.patch("sys.stdout", stdout),
                mock.patch.dict("os.environ", {"OPENAI_API_KEY": "token"}, clear=False),
            ):
                result = main(["swarm", "--config", str(config_path)])

            self.assertEqual(0, result)
            self.assertIn("Swarm preflight", stdout.getvalue())

            run_dir = runs_root(repo_dir) / "2026-04-06_121500"
            shared_manifest = (run_dir / "resources" / "shared" / "manifest.md").read_text(
                encoding="utf-8"
            )
            run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

            self.assertIn("foreign-reference.md", shared_manifest)
            self.assertEqual(str(config_path.resolve()), run_json["config_path"])

    def test_swarm_can_load_external_env_file_for_foreign_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "target-repo"
            external_dir = root / "external-config"
            config_path = external_dir / "swarm.toml"
            env_file_path = external_dir / ".env"
            stdout = io.StringIO()
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            _write_prompt_tree(external_dir)
            _write(config_path, _user_config_text())
            _write(env_file_path, "OPENAI_API_KEY=env-file-token")
            identity = RepoIdentity(
                repo_name=repo_dir.name,
                repo_key=f"{repo_dir.name}_deadbeef",
                source_kind="repo_path",
                source_value=str(repo_dir.resolve()),
                repo_dir=repo_dir.resolve(),
            )

            def _provider_from_loaded_config(loaded):
                self.assertEqual("env-file-token", loaded.resolved_env["OPENAI_API_KEY"])
                return BackgroundSequenceProvider(
                    [
                        {
                            "trust_boundaries": ["api boundary"],
                            "risky_sinks": ["sql write path"],
                            "auth_assumptions": ["session cookie is trusted"],
                            "hot_paths": ["app/service.py"],
                            "notes": ["watch org scoping"],
                        }
                    ]
                )

            with (
                mock.patch("cli.Path.cwd", return_value=repo_dir),
                mock.patch("cli._make_run_id", return_value="2026-04-06_121500"),
                mock.patch(
                    "cli.OpenAIResponsesProvider.from_loaded_config",
                    side_effect=_provider_from_loaded_config,
                ),
                mock.patch("cli.resolve_repo_identity", return_value=identity),
                mock.patch("swarm.resolve_repo_identity", return_value=identity),
                mock.patch("builtins.input", side_effect=self._input_mock(["n", "y", "y", "n"], stdout)),
                mock.patch("sys.stdout", stdout),
                mock.patch.dict("os.environ", {"AWDIT_DATA_ROOT": str(self.data_root)}, clear=True),
            ):
                result = main(["swarm", "--config", str(config_path), "--env-file", str(env_file_path)])

            self.assertEqual(0, result)
            self.assertIn("Swarm preflight", stdout.getvalue())

            run_json = json.loads(
                ((runs_root(repo_dir) / "2026-04-06_121500") / "run.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(str(config_path.resolve()), run_json["config_path"])

    def test_swarm_checked_in_generic_config_finds_foreign_repo_code_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "target-repo"
            stdout = io.StringIO()
            checked_in_config = Path(__file__).resolve().parents[1] / "config" / "config.toml"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            _write(repo_dir / "tests" / "test_service.py", "def test_ok():\n    assert True\n")
            _write(repo_dir / ".gitignore", ".env\n")
            identity = RepoIdentity(
                repo_name=repo_dir.name,
                repo_key=f"{repo_dir.name}_deadbeef",
                source_kind="repo_path",
                source_value=str(repo_dir.resolve()),
                repo_dir=repo_dir.resolve(),
            )

            with (
                mock.patch("cli.Path.cwd", return_value=repo_dir),
                mock.patch("cli._make_run_id", return_value="2026-04-06_121500"),
                mock.patch(
                    "cli.OpenAIResponsesProvider.from_loaded_config",
                    return_value=BackgroundSequenceProvider(
                        [
                            {
                                "trust_boundaries": ["api boundary"],
                                "risky_sinks": ["sql write path"],
                                "auth_assumptions": ["session cookie is trusted"],
                                "hot_paths": ["app/service.py"],
                                "notes": ["watch org scoping"],
                            }
                        ]
                    ),
                ),
                mock.patch("cli.resolve_repo_identity", return_value=identity),
                mock.patch("swarm.resolve_repo_identity", return_value=identity),
                mock.patch("builtins.input", side_effect=self._input_mock(["n", "y", "y", "n"], stdout)),
                mock.patch("sys.stdout", stdout),
                mock.patch.dict("os.environ", {"OPENAI_API_KEY": "token"}, clear=False),
            ):
                result = main(["swarm", "--config", str(checked_in_config)])

            self.assertEqual(0, result)
            run_json = json.loads(
                ((runs_root(repo_dir) / "2026-04-06_121500") / "run.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(str(checked_in_config.resolve()), run_json["config_path"])
            self.assertIn("app/service.py", run_json["eligible_files"])
            self.assertIn("tests/test_service.py", run_json["eligible_files"])
            self.assertNotEqual([".gitignore"], run_json["eligible_files"])

    def test_swarm_warns_when_repo_wide_scope_ratio_is_below_20_percent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            stdout = io.StringIO()
            checked_in_config = Path(__file__).resolve().parents[1] / "config" / "config.toml"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            _write(repo_dir / ".gitignore", ".env\n")
            for index in range(10):
                _write(repo_dir / "docs" / f"note_{index}.md", f"note {index}\n")
            identity = RepoIdentity(
                repo_name=repo_dir.name,
                repo_key=f"{repo_dir.name}_deadbeef",
                source_kind="repo_path",
                source_value=str(repo_dir.resolve()),
                repo_dir=repo_dir.resolve(),
            )

            with (
                mock.patch("cli.Path.cwd", return_value=repo_dir),
                mock.patch("cli._make_run_id", return_value="2026-04-06_121500"),
                mock.patch(
                    "cli.OpenAIResponsesProvider.from_loaded_config",
                    return_value=BackgroundSequenceProvider(
                        [
                            {
                                "trust_boundaries": ["api boundary"],
                                "risky_sinks": ["sql write path"],
                                "auth_assumptions": ["session cookie is trusted"],
                                "hot_paths": ["app/service.py"],
                                "notes": ["watch org scoping"],
                            }
                        ]
                    ),
                ),
                mock.patch("cli.resolve_repo_identity", return_value=identity),
                mock.patch("swarm.resolve_repo_identity", return_value=identity),
                mock.patch("builtins.input", side_effect=self._input_mock(["n", "y", "y", "y", "n"], stdout)),
                mock.patch("sys.stdout", stdout),
                mock.patch.dict("os.environ", {"OPENAI_API_KEY": "token"}, clear=False),
            ):
                result = main(["swarm", "--config", str(checked_in_config)])

            self.assertEqual(0, result)
            output = stdout.getvalue()
            self.assertIn("Tracked files discovered: 12", output)
            self.assertIn("Eligible files discovered: 2", output)
            self.assertIn("Eligible/tracked ratio: 16.67%", output)
            self.assertIn("Continue despite narrow scope? [y/N]", output)
            self.assertLess(
                output.index("Continue despite narrow scope? [y/N]"),
                output.index("Launch swarm? [Y/n]"),
            )

            run_dir = runs_root(repo_dir) / "2026-04-06_121500"
            run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            digest = (run_dir / "derived_context" / "swarm_digest.md").read_text(encoding="utf-8")

            self.assertTrue(run_json["scope_diagnostics"]["warning_triggered"])
            self.assertEqual(12, run_json["scope_diagnostics"]["tracked_file_count"])
            self.assertEqual(2, run_json["scope_diagnostics"]["eligible_file_count"])
            self.assertEqual([".gitignore", "app/service.py"], run_json["scope_diagnostics"]["sampled_eligible_files"])
            self.assertIn("## Scope diagnostics", digest)
            self.assertIn("- Eligible/tracked ratio: `16.67%`", digest)
            self.assertIn("- Narrow-scope warning: `yes`", digest)

    def test_swarm_does_not_warn_when_repo_wide_scope_ratio_is_at_least_20_percent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            stdout = io.StringIO()
            checked_in_config = Path(__file__).resolve().parents[1] / "config" / "config.toml"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            _write(repo_dir / ".gitignore", ".env\n")
            for index in range(8):
                _write(repo_dir / "docs" / f"note_{index}.md", f"note {index}\n")
            identity = RepoIdentity(
                repo_name=repo_dir.name,
                repo_key=f"{repo_dir.name}_deadbeef",
                source_kind="repo_path",
                source_value=str(repo_dir.resolve()),
                repo_dir=repo_dir.resolve(),
            )

            with (
                mock.patch("cli.Path.cwd", return_value=repo_dir),
                mock.patch("cli._make_run_id", return_value="2026-04-06_121500"),
                mock.patch(
                    "cli.OpenAIResponsesProvider.from_loaded_config",
                    return_value=BackgroundSequenceProvider(
                        [
                            {
                                "trust_boundaries": ["api boundary"],
                                "risky_sinks": ["sql write path"],
                                "auth_assumptions": ["session cookie is trusted"],
                                "hot_paths": ["app/service.py"],
                                "notes": ["watch org scoping"],
                            }
                        ]
                    ),
                ),
                mock.patch("cli.resolve_repo_identity", return_value=identity),
                mock.patch("swarm.resolve_repo_identity", return_value=identity),
                mock.patch("builtins.input", side_effect=self._input_mock(["n", "y", "y", "n"], stdout)),
                mock.patch("sys.stdout", stdout),
                mock.patch.dict("os.environ", {"OPENAI_API_KEY": "token"}, clear=False),
            ):
                result = main(["swarm", "--config", str(checked_in_config)])

            self.assertEqual(0, result)
            output = stdout.getvalue()
            self.assertIn("Tracked files discovered: 10", output)
            self.assertIn("Eligible files discovered: 2", output)
            self.assertIn("Eligible/tracked ratio: 20.00%", output)
            self.assertNotIn("Continue despite narrow scope?", output)

            run_json = json.loads(
                ((runs_root(repo_dir) / "2026-04-06_121500") / "run.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(run_json["scope_diagnostics"]["warning_triggered"])

    def test_swarm_prints_live_worker_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            loaded = self._loaded_config(repo_dir)

            result, output = self._run_swarm(
                repo_dir,
                loaded,
                BackgroundSequenceProvider(
                    [
                        {
                            "trust_boundaries": ["api boundary"],
                            "risky_sinks": ["sql write path"],
                            "auth_assumptions": ["session cookie is trusted"],
                            "hot_paths": ["app/service.py"],
                            "notes": ["watch org scoping"],
                        },
                        {
                            "outcome": "finding",
                            "severity_bucket": "medium",
                            "claim": "seed claim",
                            "evidence": ["app/service.py:1"],
                            "related_files": [],
                            "notes": [],
                        },
                        {
                            "outcome": "reportable",
                            "proof_state": "written_proof",
                            "claim": "seed claim",
                            "summary": "proof summary",
                            "preconditions": [],
                            "repro_steps": ["step 1"],
                            "citations": ["app/service.py:1"],
                            "notes": [],
                            "filter_reason": "",
                        },
                    ]
                ),
                ["n", "y", "y", "y", "y"],
            )

            self.assertEqual(0, result)
            self.assertIn("[* claim worker CLAIM-001 started: inspect app/service.py *]", output)
            self.assertIn("[* claim worker CLAIM-001 completed: app/service.py (", output)
            self.assertIn("Launching swarm batch...\n\nSweep stage started: 1 file worker queued.", output)
            self.assertIn("Verify stage started: 1 case worker queued.", output)
            self.assertIn(
                "Verify stage started: 1 case worker queued.\n[* verify worker CASE-001 started: verify promoted case for app/service.py *]",
                output,
            )
            self.assertIn(
                "[* verify worker CASE-001 started: verify promoted case for app/service.py *]",
                output,
            )
            self.assertIn("[* verify worker CASE-001 completed: app/service.py (", output)
            self.assertIn("Swarm complete. 1 verified finding, 0 filtered.", output)
            self.assertIn("Open this:", output)
            _assert_no_triple_newlines(self, output)

    def test_swarm_edit_regenerates_danger_map_and_appends_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            loaded = self._loaded_config(repo_dir)

            result, output = self._run_swarm(
                repo_dir,
                loaded,
                BackgroundSequenceProvider(
                    [
                        {
                            "trust_boundaries": ["first boundary"],
                            "risky_sinks": ["first sink"],
                            "auth_assumptions": ["first auth"],
                            "hot_paths": ["first/path.py"],
                            "notes": ["first note"],
                        },
                        {
                            "trust_boundaries": ["updated boundary"],
                            "risky_sinks": ["updated sink"],
                            "auth_assumptions": ["updated auth"],
                            "hot_paths": ["updated/path.py"],
                            "notes": ["updated note"],
                        },
                    ]
                ),
                ["n", "e", "focus on auth boundaries", "y", "y", "y", "y"],
            )

            self.assertEqual(0, result)
            self.assertIn("Updated repo danger map ready:", output)

            repo_dir_root = repos_root(repo_dir) / "repo_deadbeef"
            payload = json.loads((repo_dir_root / "danger_map.json").read_text(encoding="utf-8"))
            comments = (repo_dir_root / "memory" / "repo_comments.md").read_text(encoding="utf-8")

            self.assertEqual(["updated boundary"], payload["trust_boundaries"])
            self.assertEqual(["focus on auth boundaries"], payload["guidance"])
            self.assertIn("focus on auth boundaries", comments)

    def test_swarm_refresh_reuses_saved_repo_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            loaded = self._loaded_config(repo_dir)

            first_provider = BackgroundSequenceProvider(
                [
                    {
                        "trust_boundaries": ["first boundary"],
                        "risky_sinks": ["first sink"],
                        "auth_assumptions": ["first auth"],
                        "hot_paths": ["first/path.py"],
                        "notes": ["first note"],
                    },
                    {
                        "trust_boundaries": ["edited boundary"],
                        "risky_sinks": ["edited sink"],
                        "auth_assumptions": ["edited auth"],
                        "hot_paths": ["edited/path.py"],
                        "notes": ["edited note"],
                    },
                ]
            )
            second_provider = BackgroundSequenceProvider(
                [
                    {
                        "trust_boundaries": ["refreshed boundary"],
                        "risky_sinks": ["refreshed sink"],
                        "auth_assumptions": ["refreshed auth"],
                        "hot_paths": ["refreshed/path.py"],
                        "notes": ["refreshed note"],
                    }
                ]
            )

            first_result, _ = self._run_swarm(
                repo_dir,
                loaded,
                first_provider,
                ["n", "e", "focus on auth boundaries", "y", "y", "y", "y"],
                run_id="2026-04-06_121500",
            )
            self.assertEqual(0, first_result)

            second_result, output = self._run_swarm(
                repo_dir,
                loaded,
                second_provider,
                ["n", "y", "y", "y", "y", "y"],
                run_id="2026-04-06_121600",
            )

            self.assertEqual(0, second_result)
            self.assertIn("Existing repo danger map found:", output)
            self.assertIn("Updated repo danger map ready:", output)

            payload = json.loads(
                (repos_root(repo_dir) / "repo_deadbeef" / "danger_map.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(["focus on auth boundaries"], payload["guidance"])

    def test_swarm_stages_shared_resources_and_writes_preflight_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(
                repo_dir / "config" / "resources" / "shared" / "refund-boundaries.md",
                "shared note",
            )
            loaded = self._loaded_config(repo_dir)

            result, output = self._run_swarm(
                repo_dir,
                loaded,
                BackgroundSequenceProvider(
                    [
                        {
                            "trust_boundaries": ["api boundary"],
                            "risky_sinks": ["sql write path"],
                            "auth_assumptions": ["session cookie is trusted"],
                            "hot_paths": ["app/routes.py"],
                            "notes": ["watch org scoping"],
                        }
                    ]
                ),
                ["n", "y", "y", "y", "y"],
            )

            self.assertEqual(0, result)
            self.assertIn("Swarm preflight", output)
            self.assertIn("Shared resources selected for this run", output)
            self.assertIn("Launching swarm batch...", output)
            self.assertIn("Preset: safe", output)
            self.assertIn("Budget mode: enforced", output)
            self.assertIn("Claim parallelism: 2", output)
            self.assertIn("Verify parallelism: 1", output)
            self.assertIn("Rate-limit retries: 3", output)
            self.assertIn("Verify stage: read-only validation", output)
            self.assertIn("verified findings, grouped duplicates", output)
            self.assertIn("Tool trace log:", output)
            self.assertIn("Usage summary:", output)

            run_dir = runs_root(repo_dir) / "2026-04-06_121500"
            self.assertTrue((run_dir / "prompts" / "swarm_danger_map.md").exists())
            self.assertTrue((run_dir / "prompts" / "swarm_seed.md").exists())
            self.assertTrue((run_dir / "prompts" / "swarm_proof.md").exists())
            self.assertTrue((run_dir / "prompts" / "swarm_prompt_bundle.json").exists())
            self.assertTrue((run_dir / "resources" / "shared" / "manifest.md").exists())
            self.assertTrue((run_dir / "derived_context" / "swarm_digest.md").exists())
            self.assertTrue((run_dir / "swarm" / "debug" / "usage_summary.json").exists())
            # case_groups.md is only written when at least one case exists; this
            # scenario has zero eligible files, so it is intentionally absent.
            self.assertFalse((run_dir / "swarm" / "debug" / "case_groups.md").exists())
            self.assertTrue((run_dir / "swarm" / "SUMMARY.md").exists())
            self.assertTrue((run_dir / "swarm" / "FINDINGS.md").exists())

            shared_manifest = (run_dir / "resources" / "shared" / "manifest.md").read_text(
                encoding="utf-8"
            )
            swarm_digest = (run_dir / "derived_context" / "swarm_digest.md").read_text(
                encoding="utf-8"
            )
            run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

            self.assertIn("refund-boundaries.md", shared_manifest)
            self.assertIn("shared-reference", shared_manifest)
            self.assertIn("Eligible file count", swarm_digest)
            self.assertIn("Danger-map reasoning: `high`", swarm_digest)
            self.assertIn("Claim reasoning: `low`", swarm_digest)
            self.assertIn("Verify reasoning: `medium`", swarm_digest)
            self.assertEqual("swarm", run_json["mode"])
            self.assertIn("prompt_bundle", run_json["swarm"])
            self.assertEqual("safe", run_json["swarm"]["mode"]["preset"])
            self.assertEqual(2, run_json["swarm"]["parallelism"]["seed"])
            self.assertEqual(1, run_json["swarm"]["parallelism"]["proof"])
            self.assertEqual(3, run_json["swarm"]["retries"]["rate_limits"])
            self.assertEqual(
                str(run_dir / "swarm" / "debug" / "usage_summary.json"),
                run_json["swarm"]["usage_summary"],
            )
            self.assertEqual(
                str(run_dir / "swarm" / "debug" / "tool_trace.jsonl"),
                run_json["swarm"]["tool_trace_log"],
            )
            self.assertEqual(
                {
                    "danger_map": "high",
                    "seed": "low",
                    "proof": "medium",
                },
                run_json["swarm"]["reasoning"],
            )

    def test_swarm_preflight_warns_when_peak_claim_estimate_exceeds_budget_in_no_limit_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(
                repo_dir / "app" / "large.py",
                "payload = " + repr("A" * 3000),
            )
            loaded = self._loaded_config_from_text(
                repo_dir,
                _user_config_text()
                .replace('mode = "enforced"', 'mode = "advisory"', 1)
                .replace("tokens = 120000", "tokens = 100", 1),
            )

            result, output = self._run_swarm(
                repo_dir,
                loaded,
                BackgroundSequenceProvider(
                    [
                        {
                            "trust_boundaries": ["api boundary"],
                            "risky_sinks": ["sql write path"],
                            "auth_assumptions": ["session cookie is trusted"],
                            "hot_paths": ["app/large.py"],
                            "notes": ["watch org scoping"],
                        },
                        {
                            "outcome": "no_finding",
                            "severity_bucket": "none",
                            "claim": "",
                            "evidence": [],
                            "related_files": [],
                            "notes": [],
                        },
                    ]
                ),
                ["n", "y", "y", "y", "y"],
            )

            self.assertEqual(0, result)
            self.assertIn(
                "Warning: peak claim estimate exceeds the configured advisory budget.",
                output,
            )
            self.assertIn("Largest claim request: app/large.py", output)

    def test_swarm_blocks_missing_local_shared_resources_before_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            loaded = self._loaded_config_from_text(
                repo_dir,
                _user_config_text().replace(
                    'include = ["https://example.com/shared-reference"]',
                    'include = ["missing/shared-note.md"]',
                ),
            )

            result, output = self._run_swarm(
                repo_dir,
                loaded,
                BackgroundSequenceProvider(
                    [
                        {
                            "trust_boundaries": ["api boundary"],
                            "risky_sinks": ["sql write path"],
                            "auth_assumptions": ["session cookie is trusted"],
                            "hot_paths": ["app/routes.py"],
                            "notes": ["watch org scoping"],
                        }
                    ]
                ),
                ["n", "y", "y", "n"],
            )

            self.assertEqual(0, result)
            self.assertIn("Cannot continue with missing local resources:", output)
            self.assertIn("Swarm canceled before launch.", output)
            self.assertNotIn("Swarm preflight", output)

            with sqlite3.connect(state_root(repo_dir) / "awdit.db") as connection:
                row = connection.execute(
                    "SELECT status FROM runs WHERE run_id = ?",
                    ("2026-04-06_121500",),
                ).fetchone()
            self.assertEqual(("canceled",), row)

    def test_swarm_stages_config_relative_docs_without_missing_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "docs" / "architecture.md", "architecture")
            _write(repo_dir / "docs" / "agent-isolation-workflow.md", "workflow")
            _write(
                repo_dir / "docs" / "PROPOSED_FILE_STRUCTURE_CONFIG_BEHAVIOUR.txt",
                "structure",
            )
            config_text = _user_config_text().replace(
                'include = ["https://example.com/shared-reference"]',
                'include = ["../docs/architecture.md", "../docs/agent-isolation-workflow.md", "../docs/PROPOSED_FILE_STRUCTURE_CONFIG_BEHAVIOUR.txt"]',
            )
            loaded = self._loaded_config_from_text(repo_dir, config_text)

            result, output = self._run_swarm(
                repo_dir,
                loaded,
                BackgroundSequenceProvider(
                    [
                        {
                            "trust_boundaries": ["api boundary"],
                            "risky_sinks": ["sql write path"],
                            "auth_assumptions": ["session cookie is trusted"],
                            "hot_paths": ["app/routes.py"],
                            "notes": ["watch org scoping"],
                        }
                    ]
                ),
                ["n", "y", "y", "y", "y"],
            )

            self.assertEqual(0, result)
            self.assertIn("Shared resources selected for this run", output)

            run_dir = runs_root(repo_dir) / "2026-04-06_121500"
            shared_manifest = (run_dir / "resources" / "shared" / "manifest.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("architecture.md", shared_manifest)
            self.assertIn("agent-isolation-workflow.md", shared_manifest)
            self.assertIn("PROPOSED_FILE_STRUCTURE_CONFIG_BEHAVIOUR.txt", shared_manifest)
            self.assertNotIn("(missing)", shared_manifest)

    def test_swarm_preflight_failure_marks_run_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            loaded = self._loaded_config(repo_dir)
            stdout = io.StringIO()
            identity = RepoIdentity(
                repo_name=repo_dir.name,
                repo_key="repo_deadbeef",
                source_kind="repo_path",
                source_value=str(repo_dir.resolve()),
                repo_dir=repo_dir.resolve(),
            )
            with (
                mock.patch("cli.Path.cwd", return_value=repo_dir),
                mock.patch("cli.load_effective_config", return_value=loaded),
                mock.patch("cli._make_run_id", return_value="2026-04-06_121500"),
                mock.patch(
                    "cli.OpenAIResponsesProvider.from_loaded_config",
                    return_value=BackgroundSequenceProvider(
                        [
                            {
                                "trust_boundaries": ["api boundary"],
                                "risky_sinks": ["sql write path"],
                                "auth_assumptions": ["session cookie is trusted"],
                                "hot_paths": ["app/routes.py"],
                                "notes": ["watch org scoping"],
                            }
                        ]
                    ),
                ),
                mock.patch("cli.resolve_repo_identity", return_value=identity),
                mock.patch("swarm.resolve_repo_identity", return_value=identity),
                mock.patch(
                    "cli._persist_swarm_startup_snapshot",
                    side_effect=RuntimeError("snapshot broke"),
                ),
                mock.patch(
                    "builtins.input",
                    side_effect=self._input_mock(["n", "y", "y", "y"], stdout),
                ),
                mock.patch("sys.stdout", stdout),
            ):
                result = main(["swarm"])

            self.assertEqual(1, result)
            self.assertIn("Swarm startup failed: snapshot broke", stdout.getvalue())
            with sqlite3.connect(state_root(repo_dir) / "awdit.db") as connection:
                row = connection.execute(
                    "SELECT status, completed_at FROM runs WHERE run_id = ?",
                    ("2026-04-06_121500",),
                ).fetchone()
            self.assertEqual("failed", row[0])
            self.assertIsNotNone(row[1])

    def test_swarm_preflight_excludes_git_tracked_symlink_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            outside_file = root / "shared" / "linked.py"
            _write(outside_file, "print('linked')\n")
            (repo_dir / "app").mkdir(parents=True, exist_ok=True)
            (repo_dir / "app" / "link.py").symlink_to(outside_file)
            loaded = self._loaded_config(repo_dir)

            git_result = mock.Mock(returncode=0, stdout="app/link.py\n")
            with mock.patch("swarm.subprocess.run", return_value=git_result):
                result, output = self._run_swarm(
                    repo_dir,
                    loaded,
                    BackgroundSequenceProvider(
                        [
                            {
                                "trust_boundaries": ["api boundary"],
                                "risky_sinks": ["sql write path"],
                                "auth_assumptions": ["session cookie is trusted"],
                                "hot_paths": ["app/link.py"],
                                "notes": ["watch symlink handling"],
                            },
                        ]
                    ),
                    ["n", "y", "y", "y", "n"],
                )

            self.assertEqual(0, result)
            self.assertIn("Eligible files discovered: 0", output)

            run_json = json.loads(
                ((runs_root(repo_dir) / "2026-04-06_121500") / "run.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual([], run_json["eligible_files"])

    def test_swarm_generation_failure_marks_run_failed_and_returns_error(self) -> None:
        class FailingDangerMapProvider:
            def start_background_turn(self, **kwargs):
                raise RuntimeError("synthetic swarm failure")

            def cancel_background_turn(self, handle):
                return "cancelled"

            def classify_provider_failure(self, value):
                return None

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            loaded = self._loaded_config(repo_dir)

            result, output = self._run_swarm(
                repo_dir,
                loaded,
                FailingDangerMapProvider(),
                ["n"],
            )

            self.assertEqual(1, result)
            self.assertIn(
                "Swarm startup failed: Swarm worker failure: danger_map (repo:repo_deadbeef): synthetic swarm failure",
                output,
            )
            self.assertIn("Failure diagnostics:", output)
            diagnostic_path = runs_root(repo_dir) / "2026-04-06_121500" / "swarm" / "debug" / "failure_diagnostic.json"
            self.assertTrue(diagnostic_path.exists())
            diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
            self.assertEqual(1, diagnostic["failure_count"])
            self.assertEqual("danger_map", diagnostic["failures"][0]["stage"])
            self.assertEqual("danger_map", diagnostic["failures"][0]["worker_id"])
            self.assertIn("synthetic swarm failure", diagnostic["failures"][0]["failure_message"])
            with sqlite3.connect(state_root(repo_dir) / "awdit.db") as connection:
                row = connection.execute(
                    """
                    SELECT status, failure_stage, failure_worker_id, failure_message, failure_artifact
                    FROM runs
                    WHERE run_id = ?
                    """,
                    ("2026-04-06_121500",),
                ).fetchone()
            self.assertEqual("failed", row[0])
            self.assertEqual("danger_map", row[1])
            self.assertEqual("danger_map", row[2])
            self.assertIn("synthetic swarm failure", row[3])
            self.assertTrue(row[4].endswith("failure_diagnostic.json"))


class InitConfigCliTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.data_root = _set_awdit_data_root(self)

    def test_init_config_writes_commented_grouped_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            stdout = io.StringIO()

            with (
                mock.patch("cli.Path.cwd", return_value=repo_dir),
                mock.patch("sys.stdout", stdout),
            ):
                result = main(["init-config"])

            self.assertEqual(0, result)
            config_path = repo_dir / "config" / "config.toml"
            self.assertTrue(config_path.exists())

            scaffold = config_path.read_text(encoding="utf-8")
            self.assertIn("[swarm.mode]", scaffold)
            self.assertIn("[swarm.models]", scaffold)
            self.assertIn("[swarm.budget]", scaffold)
            self.assertIn(
                '#   - "safe": default, stability-first, hard-safe continuation and launch gating',
                scaffold,
            )
            self.assertIn(
                '#       - "enforced": scheduler blocks or degrades oversized work to stay inside the budget',
                scaffold,
            )
            self.assertIn(
                '#       - "advisory": budget is reported, but faster presets may push harder',
                scaffold,
            )
            self.assertIn("Wrote grouped config scaffold:", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

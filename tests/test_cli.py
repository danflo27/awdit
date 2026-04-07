from __future__ import annotations

import io
import json
import sqlite3
import time
import textwrap
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cli import main
from config import SLOT_NAMES, load_effective_config
from paths import runs_root
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
    (prompt_dir / "swarm.md").write_text("# swarm\n", encoding="utf-8")


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

        [swarm]
        prompt_file = "prompts/swarm.md"
        sweep_model = "gpt-5.4-mini"
        proof_model = "gpt-5.4"
        eligible_file_profile = "code_config_tests"
        token_budget = 120000
        allow_no_limit = true
        """
        + "\n".join(slot_blocks)
    )


class ReviewCliTests(unittest.TestCase):
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

    def _run_swarm(
        self,
        repo_dir: Path,
        loaded,
        provider,
        inputs: list[str],
        *,
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
            result = main(["swarm"])
        return result, stdout.getvalue()

    def test_swarm_generates_and_accepts_new_danger_map(self) -> None:
        class DangerMapProvider:
            def __init__(self) -> None:
                self.calls = 0

            def start_foreground_turn(self, **kwargs):
                self.calls += 1
                return ProviderTurnResult(
                    response_id=f"resp_{self.calls}",
                    final_text=json.dumps(
                        {
                            "trust_boundaries": ["api boundary"],
                            "risky_sinks": ["sql write path"],
                            "auth_assumptions": ["session cookie is trusted"],
                            "hot_paths": ["app/routes.py"],
                            "notes": ["watch org scoping"],
                        }
                    ),
                    tool_traces=(),
                    status="completed",
                    model=kwargs["model"],
                )

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            loaded = self._loaded_config(repo_dir)

            result, output = self._run_swarm(
                repo_dir,
                loaded,
                DangerMapProvider(),
                ["n", "y"],
            )

            self.assertEqual(0, result)
            self.assertIn("Starting new swarm run...", output)
            self.assertIn("No repo danger map exists for this repository yet.", output)
            self.assertIn("Repo danger map ready:", output)
            self.assertIn("Danger-map preparation complete.", output)

            repo_dir_root = repo_dir / "repos" / "repo_deadbeef"
            self.assertTrue((repo_dir_root / "danger_map.md").exists())
            self.assertTrue((repo_dir_root / "danger_map.json").exists())
            self.assertTrue((repo_dir_root / "memory" / "repo_comments.md").exists())

            with sqlite3.connect(repo_dir / "state" / "awdit.db") as connection:
                row = connection.execute(
                    "SELECT mode, status, completed_at FROM runs WHERE run_id = ?",
                    ("2026-04-06_121500",),
                ).fetchone()
            self.assertEqual(("swarm", "danger_map_ready"), row[:2])
            self.assertIsNone(row[2])

    def test_swarm_edit_regenerates_danger_map_and_appends_guidance(self) -> None:
        class DangerMapProvider:
            def __init__(self) -> None:
                self.payloads = [
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

            def start_foreground_turn(self, **kwargs):
                payload = self.payloads.pop(0)
                return ProviderTurnResult(
                    response_id=f"resp_{len(self.payloads)}",
                    final_text=json.dumps(payload),
                    tool_traces=(),
                    status="completed",
                    model=kwargs["model"],
                )

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            loaded = self._loaded_config(repo_dir)

            result, output = self._run_swarm(
                repo_dir,
                loaded,
                DangerMapProvider(),
                ["n", "e", "focus on auth boundaries", "y"],
            )

            self.assertEqual(0, result)
            self.assertIn("Updated repo danger map ready:", output)

            repo_dir_root = repo_dir / "repos" / "repo_deadbeef"
            payload = json.loads((repo_dir_root / "danger_map.json").read_text(encoding="utf-8"))
            comments = (repo_dir_root / "memory" / "repo_comments.md").read_text(encoding="utf-8")

            self.assertEqual(["updated boundary"], payload["trust_boundaries"])
            self.assertEqual(["focus on auth boundaries"], payload["guidance"])
            self.assertIn("focus on auth boundaries", comments)

    def test_swarm_refresh_reuses_saved_repo_guidance(self) -> None:
        class DangerMapProvider:
            def __init__(self, payloads: list[dict[str, object]]) -> None:
                self.payloads = list(payloads)

            def start_foreground_turn(self, **kwargs):
                payload = self.payloads.pop(0)
                return ProviderTurnResult(
                    response_id=f"resp_{len(self.payloads)}",
                    final_text=json.dumps(payload),
                    tool_traces=(),
                    status="completed",
                    model=kwargs["model"],
                )

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            loaded = self._loaded_config(repo_dir)

            first_provider = DangerMapProvider(
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
            second_provider = DangerMapProvider(
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
                ["n", "e", "focus on auth boundaries", "y"],
                run_id="2026-04-06_121500",
            )
            self.assertEqual(0, first_result)

            second_result, output = self._run_swarm(
                repo_dir,
                loaded,
                second_provider,
                ["n", "y", "y"],
                run_id="2026-04-06_121600",
            )

            self.assertEqual(0, second_result)
            self.assertIn("Existing repo danger map found:", output)
            self.assertIn("Updated repo danger map ready:", output)

            payload = json.loads(
                (repo_dir / "repos" / "repo_deadbeef" / "danger_map.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(["focus on auth boundaries"], payload["guidance"])

    def test_swarm_generation_failure_marks_run_failed_and_returns_error(self) -> None:
        class FailingDangerMapProvider:
            def start_foreground_turn(self, **kwargs):
                raise RuntimeError("synthetic swarm failure")

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
            self.assertIn("Swarm startup failed: synthetic swarm failure", output)
            with sqlite3.connect(repo_dir / "state" / "awdit.db") as connection:
                row = connection.execute(
                    "SELECT status FROM runs WHERE run_id = ?",
                    ("2026-04-06_121500",),
                ).fetchone()
            self.assertEqual(("failed",), row)


if __name__ == "__main__":
    unittest.main()

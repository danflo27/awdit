from __future__ import annotations

import json
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from awdit.cli import RuntimeResources, _persist_run_resource_snapshot
from awdit.config import SLOT_NAMES, load_effective_config
from awdit.provider_openai import BackgroundPollResult, ProviderBackgroundHandle, ProviderTurnResult
from awdit.runtime import OneSlotRuntime


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def _write_prompt_tree(base: Path) -> None:
    prompt_dir = base / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    for slot in SLOT_NAMES:
        (prompt_dir / f"{slot}.md").write_text(f"# {slot}\n", encoding="utf-8")


def _user_config_text() -> str:
    slot_blocks = []
    for slot in SLOT_NAMES:
        default_model = "gpt-5.4-mini" if slot in {"skeptic_2", "solver_2"} else "gpt-5.4"
        slot_blocks.append(
            f"""
            [slots.{slot}]
            default_model = "{default_model}"
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

        [github]
        prefer_gh = true
        """
        + "\n".join(slot_blocks)
    )


class ImmediateProvider:
    def __init__(self) -> None:
        self.calls = 0

    def start_foreground_turn(self, **kwargs) -> ProviderTurnResult:
        self.calls += 1
        tool_output = kwargs["tool_executor"]("list_scope_files", {"limit": 10})
        return ProviderTurnResult(
            response_id=f"resp_{self.calls}",
            final_text=f"done {self.calls}\n{tool_output}",
            tool_traces=(),
            status="completed",
            model=kwargs["model"],
        )

    def start_background_turn(self, **kwargs) -> ProviderBackgroundHandle:
        return ProviderBackgroundHandle(response_id="bg_start")

    def poll_background_turn(self, **kwargs) -> BackgroundPollResult:
        return BackgroundPollResult(
            status="completed",
            response_id="bg_done",
            final_text="background done",
            tool_traces=(),
        )

    def classify_provider_failure(self, value) -> str | None:
        return None


class BackgroundProvider(ImmediateProvider):
    def __init__(self) -> None:
        super().__init__()
        self.poll_calls = 0

    def start_foreground_turn(self, **kwargs) -> ProviderTurnResult:
        raise AssertionError("foreground should not be used")

    def poll_background_turn(self, **kwargs) -> BackgroundPollResult:
        self.poll_calls += 1
        if self.poll_calls == 1:
            return BackgroundPollResult(
                status="running",
                response_id="bg_running",
                final_text="",
                tool_traces=(),
            )
        return BackgroundPollResult(
            status="completed",
            response_id="bg_done",
            final_text="background done",
            tool_traces=(),
        )


class BlockingProvider(ImmediateProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def start_foreground_turn(self, **kwargs) -> ProviderTurnResult:
        self.started.set()
        self.release.wait(timeout=5.0)
        return super().start_foreground_turn(**kwargs)


class RecoveringProvider(ImmediateProvider):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False

    def start_foreground_turn(self, **kwargs) -> ProviderTurnResult:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("provider handle lost")
        return super().start_foreground_turn(**kwargs)

    def classify_provider_failure(self, value) -> str | None:
        if "provider handle lost" in str(value):
            return str(value)
        return None


class FirstDispatchFailureProvider(ImmediateProvider):
    def start_foreground_turn(self, **kwargs) -> ProviderTurnResult:
        raise RuntimeError("provider handle lost")

    def classify_provider_failure(self, value) -> str | None:
        if "provider handle lost" in str(value):
            return str(value)
        return None


class RuntimeTests(unittest.TestCase):
    def _loaded_config(self, repo_dir: Path):
        home_dir = repo_dir.parent / "home" / ".awdit"
        user_config = home_dir / "config.toml"
        repo_config = repo_dir / "config" / "config.toml"
        _write_prompt_tree(home_dir)
        _write(user_config, _user_config_text())
        _write(repo_config, "")
        return load_effective_config(
            cwd=repo_dir,
            user_config_path=user_config,
            repo_config_path=repo_config,
            env={"OPENAI_API_KEY": "token"},
        )

    def _make_runtime(
        self,
        repo_dir: Path,
        *,
        provider,
        default_mode: str = "foreground",
    ) -> OneSlotRuntime:
        _write(repo_dir / "app" / "service.py", "print('hello')\n")
        _write(repo_dir / "docs" / "ignored.md", "ignore me\n")
        _write(repo_dir / "notes" / "shared-note.md", "shared runtime note\n")
        loaded = self._loaded_config(repo_dir)
        resources = RuntimeResources(
            shared=(str((repo_dir / "notes" / "shared-note.md").resolve()),),
            slots={slot_name: () for slot_name in SLOT_NAMES},
        )
        with mock.patch("awdit.cli._make_run_id", return_value="2026-03-29_101530"):
            snapshot = _persist_run_resource_snapshot(repo_dir, loaded, resources)
        runtime = OneSlotRuntime(
            cwd=repo_dir,
            loaded=loaded,
            run_dir=snapshot.run_dir,
            default_mode=default_mode,
            provider=provider,
            poll_interval_seconds=0.01,
        )
        self.addCleanup(lambda: runtime.request_shutdown())
        self.addCleanup(lambda: runtime.wait_for_idle(timeout_seconds=5.0))
        return runtime

    def test_reserved_epoch_exists_before_first_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime = self._make_runtime(Path(tmp_dir) / "repo", provider=ImmediateProvider())

            epoch_path = runtime.epochs_dir / f"{runtime.state.current_epoch_id}.json"
            record = json.loads(epoch_path.read_text(encoding="utf-8"))

            self.assertEqual("reserved", record["status"])
            self.assertIsNone(record["lease_started_at"])
            self.assertIsNone(record["last_heartbeat_at"])
            self.assertFalse(runtime.state.current_epoch_live)

    def test_first_dispatch_activates_epoch_and_writes_artifacts_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime = self._make_runtime(Path(tmp_dir) / "repo", provider=ImmediateProvider())

            accepted, _, dispatch_id = runtime.submit_dispatch(
                work_label="Read runtime state",
                work_key="runtime/read",
                instructions_text="Describe the repo.",
                mode="foreground",
            )
            self.assertTrue(accepted)
            self.assertIsNotNone(dispatch_id)

            record = runtime.wait_for_dispatch(dispatch_id)
            self.assertEqual("completed", record.status)
            self.assertTrue(runtime.state.current_epoch_live)
            self.assertTrue((runtime.artifacts_root / dispatch_id / "response.txt").exists())
            self.assertIsNotNone(record.checkpoint_ref)
            self.assertTrue(Path(record.checkpoint_ref).exists())
            self.assertIn("dispatch_completed", [event["event_type"] for event in runtime.recent_events()])

    def test_idle_compaction_creates_new_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime = self._make_runtime(Path(tmp_dir) / "repo", provider=ImmediateProvider())
            accepted, _, dispatch_id = runtime.submit_dispatch(
                work_label="Prime checkpoint",
                work_key="runtime/prime",
                instructions_text="Run once.",
            )
            self.assertTrue(accepted)
            runtime.wait_for_dispatch(dispatch_id)
            original_epoch = runtime.state.current_epoch_id

            message = runtime.request_compaction()

            self.assertIn("Compaction complete", message)
            self.assertNotEqual(original_epoch, runtime.state.current_epoch_id)
            new_record = json.loads(
                (runtime.epochs_dir / f"{runtime.state.current_epoch_id}.json").read_text(encoding="utf-8")
            )
            self.assertEqual("live", new_record["status"])
            self.assertEqual(runtime.state.latest_checkpoint_ref, new_record["seed_checkpoint_ref"])

    def test_active_compaction_defers_until_dispatch_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            provider = BlockingProvider()
            runtime = self._make_runtime(Path(tmp_dir) / "repo", provider=provider)
            accepted, _, dispatch_id = runtime.submit_dispatch(
                work_label="Slow work",
                work_key="runtime/slow",
                instructions_text="Block for a bit.",
            )
            self.assertTrue(accepted)
            self.assertTrue(provider.started.wait(timeout=2.0))
            original_epoch = runtime.state.current_epoch_id

            message = runtime.request_compaction()
            self.assertIn("requested", message.lower())
            self.assertEqual(original_epoch, runtime.state.current_epoch_id)

            provider.release.set()
            runtime.wait_for_dispatch(dispatch_id)
            self.assertNotEqual(original_epoch, runtime.state.current_epoch_id)

    def test_same_work_key_supersession_replaces_pending_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            provider = BlockingProvider()
            runtime = self._make_runtime(Path(tmp_dir) / "repo", provider=provider)
            accepted, _, active_id = runtime.submit_dispatch(
                work_label="Slow work",
                work_key="runtime/key",
                instructions_text="Block for a bit.",
            )
            self.assertTrue(accepted)
            self.assertTrue(provider.started.wait(timeout=2.0))

            accepted, _, pending_id = runtime.submit_dispatch(
                work_label="Pending v1",
                work_key="runtime/key",
                instructions_text="Pending one.",
            )
            self.assertTrue(accepted)
            accepted, _, replacement_id = runtime.submit_dispatch(
                work_label="Pending v2",
                work_key="runtime/key",
                instructions_text="Pending two.",
            )
            self.assertTrue(accepted)

            old_pending = runtime.wait_for_dispatch(pending_id)
            self.assertEqual("superseded", old_pending.status)
            self.assertEqual(replacement_id, runtime.state.pending_dispatch_id)

            provider.release.set()
            runtime.wait_for_idle(timeout_seconds=5.0)
            replacement = runtime.wait_for_dispatch(replacement_id)
            self.assertEqual("completed", replacement.status)
            self.assertEqual(active_id, runtime.recent_events(limit=50)[2]["dispatch_id"])

    def test_unrelated_pending_dispatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            provider = BlockingProvider()
            runtime = self._make_runtime(Path(tmp_dir) / "repo", provider=provider)
            accepted, _, _ = runtime.submit_dispatch(
                work_label="Slow work",
                work_key="runtime/key-a",
                instructions_text="Block for a bit.",
            )
            self.assertTrue(accepted)
            self.assertTrue(provider.started.wait(timeout=2.0))

            accepted, message, dispatch_id = runtime.submit_dispatch(
                work_label="Other work",
                work_key="runtime/key-b",
                instructions_text="Should reject.",
            )
            self.assertFalse(accepted)
            self.assertIsNone(dispatch_id)
            self.assertIn("Rejected", message)
            provider.release.set()
            runtime.wait_for_idle(timeout_seconds=5.0)

    def test_provider_failure_recovery_rolls_epoch_and_retries_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            provider = RecoveringProvider()
            runtime = self._make_runtime(Path(tmp_dir) / "repo", provider=provider)

            accepted, _, first_dispatch_id = runtime.submit_dispatch(
                work_label="Prime handle",
                work_key="runtime/prime",
                instructions_text="Please finish once.",
            )
            self.assertTrue(accepted)
            first_record = runtime.wait_for_dispatch(first_dispatch_id, timeout_seconds=5.0)
            self.assertEqual("completed", first_record.status)
            provider.fail_next = True

            accepted, _, dispatch_id = runtime.submit_dispatch(
                work_label="Recover me",
                work_key="runtime/recover",
                instructions_text="Please retry after provider failure.",
            )
            self.assertTrue(accepted)
            record = runtime.wait_for_dispatch(dispatch_id, timeout_seconds=5.0)

            self.assertEqual("completed", record.status)
            recovery_path = runtime.artifacts_root / dispatch_id / "recovery.json"
            self.assertTrue(recovery_path.exists())
            recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
            self.assertNotEqual(recovery["failed_epoch_id"], recovery["recovered_epoch_id"])

    def test_first_dispatch_failure_does_not_fake_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime = self._make_runtime(
                Path(tmp_dir) / "repo",
                provider=FirstDispatchFailureProvider(),
            )

            accepted, _, dispatch_id = runtime.submit_dispatch(
                work_label="Fail fast",
                work_key="runtime/fail",
                instructions_text="Please fail immediately.",
            )
            self.assertTrue(accepted)
            record = runtime.wait_for_dispatch(dispatch_id, timeout_seconds=5.0)

            self.assertEqual("failed", record.status)
            self.assertFalse((runtime.artifacts_root / dispatch_id / "recovery.json").exists())

    def test_quit_is_rejected_while_dispatch_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            provider = BlockingProvider()
            runtime = self._make_runtime(Path(tmp_dir) / "repo", provider=provider)

            accepted, _, _ = runtime.submit_dispatch(
                work_label="Slow work",
                work_key="runtime/quit",
                instructions_text="Block for a bit.",
            )
            self.assertTrue(accepted)
            self.assertTrue(provider.started.wait(timeout=2.0))

            allowed, message = runtime.request_shutdown()
            self.assertFalse(allowed)
            self.assertIn("Quit refused", message)
            provider.release.set()
            runtime.wait_for_idle(timeout_seconds=5.0)

    def test_runtime_tools_read_live_repo_and_staged_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime = self._make_runtime(Path(tmp_dir) / "repo", provider=ImmediateProvider())

            listed = json.loads(runtime._tool_list_scope_files({"limit": 20}))
            self.assertIn("app/service.py", listed["paths"])
            staged_paths = [path for path in listed["paths"] if "resources/shared/staged" in path]
            self.assertTrue(staged_paths)

            file_data = json.loads(runtime._tool_read_file({"path": "app/service.py"}))
            self.assertIn("print('hello')", file_data["content"])

            staged_data = json.loads(runtime._tool_read_file({"path": staged_paths[0]}))
            self.assertIn("shared runtime note", staged_data["content"])

            matches = json.loads(runtime._tool_search_text({"query": "hello"}))
            self.assertEqual("app/service.py", matches["matches"][0]["path"])


if __name__ == "__main__":
    unittest.main()

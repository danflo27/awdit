from __future__ import annotations

import json
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from config import load_effective_config
from provider_openai import (
    BackgroundPollResult,
    ProviderBackgroundHandle,
    ProviderToolCall,
    ProviderTurnResult,
    ToolTraceRecord,
)
from state_db import load_learned_model_limit, save_learned_model_limit
from swarm import (
    RepoReadOnlyTools,
    SwarmRunMetrics,
    SwarmSeedResult,
    SwarmStageAbort,
    SwarmWorkerFailure,
    SwarmWorkerJob,
    freeze_swarm_prompt_bundle,
    generate_danger_map,
    list_eligible_swarm_files,
    promote_issue_candidates,
    run_background_swarm_workers,
    run_swarm_sweep,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def _write_prompt_tree(base: Path) -> None:
    prompt_dir = base / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    for slot_name in (
        "hunter_1",
        "hunter_2",
        "skeptic_1",
        "skeptic_2",
        "referee_1",
        "referee_2",
        "solver_1",
        "solver_2",
    ):
        (prompt_dir / f"{slot_name}.md").write_text(f"# {slot_name}\n", encoding="utf-8")
    (prompt_dir / "swarm_danger_map.md").write_text("# frozen danger map\n", encoding="utf-8")
    (prompt_dir / "swarm_seed.md").write_text(
        "\n".join(
            [
                "# frozen seed prompt",
                "Hint: {{target_file}}",
                "Write to {{output_path}}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (prompt_dir / "swarm_proof.md").write_text("# frozen proof prompt\n", encoding="utf-8")


def _config_text() -> str:
    return """
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

    [slots.hunter_1]
    default_model = "gpt-5.4-mini"
    reasoning_effort = "medium"
    prompt_file = "prompts/hunter_1.md"

    [slots.hunter_2]
    default_model = "gpt-5.4-mini"
    reasoning_effort = "medium"
    prompt_file = "prompts/hunter_2.md"

    [slots.skeptic_1]
    default_model = "gpt-5.4"
    reasoning_effort = "medium"
    prompt_file = "prompts/skeptic_1.md"

    [slots.skeptic_2]
    default_model = "gpt-5.4-mini"
    reasoning_effort = "medium"
    prompt_file = "prompts/skeptic_2.md"

    [slots.referee_1]
    default_model = "gpt-5.4"
    reasoning_effort = "medium"
    prompt_file = "prompts/referee_1.md"

    [slots.referee_2]
    default_model = "gpt-5.4-mini"
    reasoning_effort = "medium"
    prompt_file = "prompts/referee_2.md"

    [slots.solver_1]
    default_model = "gpt-5.4"
    reasoning_effort = "medium"
    prompt_file = "prompts/solver_1.md"

    [slots.solver_2]
    default_model = "gpt-5.4-mini"
    reasoning_effort = "medium"
    prompt_file = "prompts/solver_2.md"
    """


class SequenceProvider:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self._pending_payloads = list(payloads)
        self._response_payloads: dict[str, dict[str, object]] = {}
        self.start_calls: list[dict[str, object]] = []
        self.cancelled: list[str] = []

    def start_background_turn(self, **kwargs):
        if not self._pending_payloads:
            raise AssertionError("No pending payloads left.")
        response_id = f"bg_{len(self.start_calls) + 1}"
        self._response_payloads[response_id] = self._pending_payloads.pop(0)
        self.start_calls.append(kwargs)
        return ProviderBackgroundHandle(response_id=response_id)

    def poll_background_turn(self, **kwargs):
        response_id = kwargs["handle"].response_id
        payload = self._response_payloads.pop(response_id)
        return BackgroundPollResult(
            status="completed",
            response_id=response_id,
            final_text=json.dumps(payload),
            tool_traces=(),
        )

    def cancel_background_turn(self, handle):
        self.cancelled.append(handle.response_id)
        self._response_payloads.pop(handle.response_id, None)
        return "cancelled"

    def classify_provider_failure(self, value):
        return None


class ContinuationToolProvider:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, object]] = []
        self.continue_calls: list[dict[str, object]] = []

    def start_background_turn(self, **kwargs):
        self.start_calls.append(kwargs)
        return ProviderBackgroundHandle(response_id="bg_1")

    def continue_background_turn(self, **kwargs):
        self.continue_calls.append(kwargs)
        return ProviderBackgroundHandle(response_id="bg_2")

    def poll_background_turn(self, **kwargs):
        response_id = kwargs["handle"].response_id
        event_callback = kwargs.get("event_callback")
        if response_id == "bg_1":
            if event_callback is not None:
                event_callback(
                    "tool_calls_requested",
                    {
                        "response_id": response_id,
                        "tool_calls": [
                            {
                                "call_id": "call_read",
                                "name": "read_file",
                                "arguments": {
                                    "path": "app/service.py",
                                    "start_line": 1,
                                    "max_lines": 2,
                                },
                            },
                            {
                                "call_id": "call_list",
                                "name": "list_scope_files",
                                "arguments": {
                                    "path_glob": "tests/**",
                                },
                            },
                        ],
                    },
                )
            return BackgroundPollResult(
                status="awaiting_continuation",
                response_id=response_id,
                final_text="",
                tool_traces=(
                    ToolTraceRecord(
                        call_id="call_read",
                        name="read_file",
                        arguments={"path": "app/service.py", "start_line": 1, "max_lines": 2},
                        output='{"path":"app/service.py"}',
                        output_meta={
                            "chars": 24,
                            "start_line": 1,
                            "end_line": 2,
                            "truncated_before": False,
                            "truncated_after": False,
                            "raw_line_count": 3,
                            "raw_char_count": 30,
                        },
                        response_id=response_id,
                    ),
                    ToolTraceRecord(
                        call_id="call_list",
                        name="list_scope_files",
                        arguments={"path_glob": "tests/**"},
                        output='{"count":1}',
                        output_meta={"chars": 11},
                        response_id=response_id,
                    ),
                ),
                continuation_input=(
                    {"type": "function_call_output", "call_id": "call_read", "output": '{"ok":true}'},
                    {"type": "function_call_output", "call_id": "call_list", "output": '{"ok":true}'},
                ),
                tool_calls=(
                    ProviderToolCall(
                        call_id="call_read",
                        name="read_file",
                        arguments={"path": "app/service.py", "start_line": 1, "max_lines": 2},
                    ),
                    ProviderToolCall(
                        call_id="call_list",
                        name="list_scope_files",
                        arguments={"path_glob": "tests/**"},
                    ),
                ),
            )
        return BackgroundPollResult(
            status="completed",
            response_id=response_id,
            final_text=json.dumps({"ok": True}),
            tool_traces=(),
        )

    def cancel_background_turn(self, handle):
        return "cancelled"

    def classify_provider_failure(self, value):
        return None


class RetryProvider:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, object]] = []
        self.poll_count = 0

    def start_background_turn(self, **kwargs):
        response_id = f"bg_{len(self.start_calls) + 1}"
        self.start_calls.append(kwargs)
        return ProviderBackgroundHandle(response_id=response_id)

    def poll_background_turn(self, **kwargs):
        self.poll_count += 1
        response_id = kwargs["handle"].response_id
        if self.poll_count == 1:
            return BackgroundPollResult(
                status="failed",
                response_id=response_id,
                final_text="",
                tool_traces=(),
                failure_message="transient",
            )
        return BackgroundPollResult(
            status="completed",
            response_id=response_id,
            final_text=json.dumps({"ok": True}),
            tool_traces=(),
        )

    def cancel_background_turn(self, handle):
        return "cancelled"

    def classify_provider_failure(self, value):
        return None


class MaxInFlightProvider:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self._pending_payloads = list(payloads)
        self._response_payloads: dict[str, dict[str, object]] = {}
        self._active_handles: set[str] = set()
        self.start_calls: list[dict[str, object]] = []
        self.max_in_flight = 0

    def start_background_turn(self, **kwargs):
        if not self._pending_payloads:
            raise AssertionError("No pending payloads left.")
        response_id = f"bg_{len(self.start_calls) + 1}"
        self._response_payloads[response_id] = self._pending_payloads.pop(0)
        self._active_handles.add(response_id)
        self.max_in_flight = max(self.max_in_flight, len(self._active_handles))
        self.start_calls.append(kwargs)
        return ProviderBackgroundHandle(response_id=response_id)

    def poll_background_turn(self, **kwargs):
        response_id = kwargs["handle"].response_id
        self._active_handles.discard(response_id)
        payload = self._response_payloads.pop(response_id)
        return BackgroundPollResult(
            status="completed",
            response_id=response_id,
            final_text=json.dumps(payload),
            tool_traces=(),
        )

    def cancel_background_turn(self, handle):
        self._active_handles.discard(handle.response_id)
        return "cancelled"

    def classify_provider_failure(self, value):
        return None


class RateLimitThenSuccessProvider:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, object]] = []
        self._response_payloads: dict[str, dict[str, object]] = {}

    def start_background_turn(self, **kwargs):
        self.start_calls.append(kwargs)
        if len(self.start_calls) == 1:
            raise RuntimeError(
                "ResponseError(code='rate_limit_exceeded', message='Rate limit reached for "
                "gpt-5.4-mini on tokens per min (TPM): Limit 200000, Used 188404, Requested "
                "19721. Please try again in 2.5s.')"
            )
        response_id = f"bg_{len(self.start_calls)}"
        self._response_payloads[response_id] = {"ok": True}
        return ProviderBackgroundHandle(response_id=response_id)

    def poll_background_turn(self, **kwargs):
        response_id = kwargs["handle"].response_id
        payload = self._response_payloads.pop(response_id)
        return BackgroundPollResult(
            status="completed",
            response_id=response_id,
            final_text=json.dumps(payload),
            tool_traces=(),
        )

    def cancel_background_turn(self, handle):
        self._response_payloads.pop(handle.response_id, None)
        return "cancelled"

    def classify_provider_failure(self, value):
        return None


class RateLimitMillisThenSuccessProvider:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, object]] = []
        self._response_payloads: dict[str, dict[str, object]] = {}

    def start_background_turn(self, **kwargs):
        self.start_calls.append(kwargs)
        if len(self.start_calls) == 1:
            raise RuntimeError(
                "ResponseError(code='rate_limit_exceeded', message='Rate limit reached for "
                "gpt-5.4-mini on tokens per min (TPM): Limit 200000, Used 188404, Requested "
                "19721. Please try again in 934ms.')"
            )
        response_id = f"bg_{len(self.start_calls)}"
        self._response_payloads[response_id] = {"ok": True}
        return ProviderBackgroundHandle(response_id=response_id)

    def poll_background_turn(self, **kwargs):
        response_id = kwargs["handle"].response_id
        payload = self._response_payloads.pop(response_id)
        return BackgroundPollResult(
            status="completed",
            response_id=response_id,
            final_text=json.dumps(payload),
            tool_traces=(),
        )

    def cancel_background_turn(self, handle):
        self._response_payloads.pop(handle.response_id, None)
        return "cancelled"

    def classify_provider_failure(self, value):
        return None


class RunningThenFailureProvider:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, object]] = []
        self.cancelled: list[str] = []

    def start_background_turn(self, **kwargs):
        response_id = f"bg_{len(self.start_calls) + 1}"
        self.start_calls.append(kwargs)
        return ProviderBackgroundHandle(response_id=response_id)

    def poll_background_turn(self, **kwargs):
        response_id = kwargs["handle"].response_id
        if response_id == "bg_1":
            return BackgroundPollResult(
                status="running",
                response_id=response_id,
                final_text="",
                tool_traces=(),
            )
        return BackgroundPollResult(
            status="failed",
            response_id=response_id,
            final_text="",
            tool_traces=(),
            failure_message="synthetic failure",
        )

    def cancel_background_turn(self, handle):
        self.cancelled.append(handle.response_id)
        return "cancelled"

    def classify_provider_failure(self, value):
        return None


class PartialSeedFailureProvider:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, object]] = []

    def start_background_turn(self, **kwargs):
        response_id = f"bg_{len(self.start_calls) + 1}"
        self.start_calls.append(kwargs)
        return ProviderBackgroundHandle(response_id=response_id)

    def poll_background_turn(self, **kwargs):
        response_id = kwargs["handle"].response_id
        if response_id == "bg_1":
            return BackgroundPollResult(
                status="completed",
                response_id=response_id,
                final_text=json.dumps(
                    {
                        "outcome": "no_finding",
                        "severity_bucket": "none",
                        "claim": "",
                        "evidence": [],
                        "related_files": [],
                        "notes": [],
                    }
                ),
                tool_traces=(),
            )
        return BackgroundPollResult(
            status="failed",
            response_id=response_id,
            final_text="",
            tool_traces=(),
            failure_message="synthetic failure",
        )

    def cancel_background_turn(self, handle):
        return "cancelled"

    def classify_provider_failure(self, value):
        return None


class UsageEventProvider:
    def __init__(self, responses: list[tuple[dict[str, object], dict[str, int]]]) -> None:
        self._pending_responses = list(responses)
        self._responses: dict[str, tuple[dict[str, object], dict[str, int]]] = {}
        self.start_calls: list[dict[str, object]] = []

    def start_background_turn(self, **kwargs):
        if not self._pending_responses:
            raise AssertionError("No pending usage responses left.")
        response_id = f"bg_{len(self.start_calls) + 1}"
        self._responses[response_id] = self._pending_responses.pop(0)
        self.start_calls.append(kwargs)
        return ProviderBackgroundHandle(response_id=response_id)

    def poll_background_turn(self, **kwargs):
        response_id = kwargs["handle"].response_id
        payload, usage = self._responses.pop(response_id)
        event_callback = kwargs.get("event_callback")
        if event_callback is not None:
            event_callback(
                "provider_usage",
                {
                    "response_id": response_id,
                    "model": kwargs["model"],
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "total_tokens": usage["total_tokens"],
                    "cached_input_tokens": usage["cached_input_tokens"],
                    "reasoning_output_tokens": usage["reasoning_output_tokens"],
                },
            )
        time.sleep(0.01)
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


class MaxInFlightUsageProvider:
    def __init__(self, usage_tokens: int) -> None:
        self._usage_tokens = usage_tokens
        self._response_payloads: dict[str, dict[str, object]] = {}
        self._active_handles: set[str] = set()
        self.start_calls: list[dict[str, object]] = []
        self.start_usage_emitted_flags: list[bool] = []
        self.max_in_flight = 0
        self.usage_emitted = False

    def start_background_turn(self, **kwargs):
        response_id = f"bg_{len(self.start_calls) + 1}"
        self._response_payloads[response_id] = {"ok": kwargs["input_text"]}
        self._active_handles.add(response_id)
        self.max_in_flight = max(self.max_in_flight, len(self._active_handles))
        self.start_usage_emitted_flags.append(self.usage_emitted)
        self.start_calls.append(kwargs)
        return ProviderBackgroundHandle(response_id=response_id)

    def poll_background_turn(self, **kwargs):
        response_id = kwargs["handle"].response_id
        event_callback = kwargs.get("event_callback")
        if event_callback is not None:
            event_callback(
                "provider_usage",
                {
                    "response_id": response_id,
                    "model": kwargs["model"],
                    "input_tokens": self._usage_tokens,
                    "output_tokens": 5,
                    "total_tokens": self._usage_tokens + 5,
                    "cached_input_tokens": 0,
                    "reasoning_output_tokens": 0,
                },
            )
        self.usage_emitted = True
        self._active_handles.discard(response_id)
        payload = self._response_payloads.pop(response_id)
        return BackgroundPollResult(
            status="completed",
            response_id=response_id,
            final_text=json.dumps(payload),
            tool_traces=(),
        )

    def cancel_background_turn(self, handle):
        self._active_handles.discard(handle.response_id)
        self._response_payloads.pop(handle.response_id, None)
        return "cancelled"

    def classify_provider_failure(self, value):
        return None


class RateLimitThenUsageProvider:
    def __init__(self, *, usage_tokens: int, message: str) -> None:
        self._usage_tokens = usage_tokens
        self._message = message
        self._response_payloads: dict[str, dict[str, object]] = {}
        self.start_calls: list[dict[str, object]] = []

    def start_background_turn(self, **kwargs):
        self.start_calls.append(kwargs)
        if len(self.start_calls) == 1:
            raise RuntimeError(self._message)
        response_id = f"bg_{len(self.start_calls)}"
        self._response_payloads[response_id] = {"ok": kwargs["input_text"]}
        return ProviderBackgroundHandle(response_id=response_id)

    def poll_background_turn(self, **kwargs):
        response_id = kwargs["handle"].response_id
        event_callback = kwargs.get("event_callback")
        if event_callback is not None:
            event_callback(
                "provider_usage",
                {
                    "response_id": response_id,
                    "model": kwargs["model"],
                    "input_tokens": self._usage_tokens,
                    "output_tokens": 5,
                    "total_tokens": self._usage_tokens + 5,
                    "cached_input_tokens": 0,
                    "reasoning_output_tokens": 0,
                },
            )
        payload = self._response_payloads.pop(response_id)
        return BackgroundPollResult(
            status="completed",
            response_id=response_id,
            final_text=json.dumps(payload),
            tool_traces=(),
        )

    def cancel_background_turn(self, handle):
        self._response_payloads.pop(handle.response_id, None)
        return "cancelled"

    def classify_provider_failure(self, value):
        return None


class DrainAfterAbortProvider:
    def __init__(
        self,
        *,
        failure_message: str,
        completed_payload: dict[str, object] | None = None,
        running_polls: int = 2,
    ) -> None:
        self._failure_message = failure_message
        self._completed_payload = completed_payload or {"ok": True}
        self._running_polls = max(1, running_polls)
        self.start_calls: list[dict[str, object]] = []
        self._poll_counts: dict[str, int] = {}
        self.cancelled: list[str] = []

    def start_background_turn(self, **kwargs):
        response_id = f"bg_{len(self.start_calls) + 1}"
        self.start_calls.append(kwargs)
        return ProviderBackgroundHandle(response_id=response_id)

    def poll_background_turn(self, **kwargs):
        response_id = kwargs["handle"].response_id
        self._poll_counts[response_id] = self._poll_counts.get(response_id, 0) + 1
        event_callback = kwargs.get("event_callback")
        if response_id == "bg_1":
            if self._poll_counts[response_id] <= self._running_polls:
                if event_callback is not None and self._poll_counts[response_id] == 1:
                    event_callback(
                        "provider_usage",
                        {
                            "response_id": response_id,
                            "model": kwargs["model"],
                            "input_tokens": 20,
                            "output_tokens": 5,
                            "total_tokens": 25,
                            "cached_input_tokens": 0,
                            "reasoning_output_tokens": 0,
                        },
                    )
                return BackgroundPollResult(
                    status="running",
                    response_id=response_id,
                    final_text="",
                    tool_traces=(),
                )
            return BackgroundPollResult(
                status="completed",
                response_id=response_id,
                final_text=json.dumps(self._completed_payload),
                tool_traces=(),
            )
        return BackgroundPollResult(
            status="failed",
            response_id=response_id,
            final_text="",
            tool_traces=(),
            failure_message=self._failure_message,
        )

    def cancel_background_turn(self, handle):
        self.cancelled.append(handle.response_id)
        return "cancelled"

    def classify_provider_failure(self, value):
        return None


class SeedStageAbortProvider(DrainAfterAbortProvider):
    def __init__(self, *, failure_message: str) -> None:
        super().__init__(
            failure_message=failure_message,
            completed_payload={
                "outcome": "no_finding",
                "severity_bucket": "none",
                "claim": "",
                "evidence": [],
                "related_files": [],
                "notes": [],
            },
        )


class SwarmTests(unittest.TestCase):
    def _loaded_config(self, repo_dir: Path):
        return self._loaded_config_from_text(repo_dir, _config_text())

    def _loaded_config_from_text(self, repo_dir: Path, config_text: str):
        config_dir = repo_dir / "config"
        _write_prompt_tree(config_dir)
        _write(config_dir / "config.toml", config_text)
        return load_effective_config(
            cwd=repo_dir,
            config_path=config_dir / "config.toml",
            env={"OPENAI_API_KEY": "token"},
        )

    def test_seed_workers_use_frozen_prompt_bundle_and_stateless_background_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            loaded = self._loaded_config(repo_dir)
            run_dir = repo_dir / "runs" / "run_1"
            run_dir.mkdir(parents=True, exist_ok=True)
            prompt_bundle = freeze_swarm_prompt_bundle(run_dir=run_dir, loaded=loaded)
            loaded.effective.swarm.prompts.seed.write_text("changed later\n", encoding="utf-8")

            swarm_digest = run_dir / "derived_context" / "swarm_digest.md"
            shared_manifest = run_dir / "resources" / "shared" / "manifest.md"
            _write(swarm_digest, "# digest\n")
            _write(shared_manifest, "# shared\n")

            provider = SequenceProvider(
                [
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
                        "summary": "Tight written proof.",
                        "preconditions": ["Reach the vulnerable endpoint."],
                        "repro_steps": [],
                        "citations": ["app/service.py:1"],
                        "notes": [],
                        "filter_reason": "",
                    }
                ]
            )

            result = run_swarm_sweep(
                cwd=repo_dir,
                loaded=loaded,
                provider=provider,
                prompt_bundle=prompt_bundle,
                run_dir=run_dir,
                swarm_digest_path=swarm_digest,
                shared_manifest_path=shared_manifest,
                eligible_files=[(repo_dir / "app" / "service.py").resolve()],
            )

            self.assertEqual(1, len(result.seed_results))
            self.assertEqual(1, len(result.issue_candidates))
            self.assertEqual(1, len(result.proof_results))
            self.assertIn("# frozen seed prompt", provider.start_calls[0]["instructions"])
            self.assertIn("Hint: app/service.py", provider.start_calls[0]["instructions"])
            self.assertIn("Write to runs/run_1/swarm/seeds", provider.start_calls[0]["instructions"])
            self.assertEqual("# frozen proof prompt\n", provider.start_calls[1]["instructions"])
            self.assertEqual("low", provider.start_calls[0]["reasoning_effort"])
            self.assertEqual("medium", provider.start_calls[1]["reasoning_effort"])
            self.assertEqual(prompt_bundle.seed.prompt_cache_key, provider.start_calls[0]["prompt_cache_key"])
            self.assertEqual(prompt_bundle.proof.prompt_cache_key, provider.start_calls[1]["prompt_cache_key"])
            self.assertIsNone(provider.start_calls[0]["previous_response_id"])
            self.assertIsNone(provider.start_calls[1]["previous_response_id"])

    def test_swarm_uses_configured_reasoning_levels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            loaded = self._loaded_config_from_text(
                repo_dir,
                _config_text().replace(
                    '[swarm.prompts]',
                    '[swarm.reasoning]\ndanger_map = "low"\nseed = "high"\nproof = "low"\n\n[swarm.prompts]',
                    1,
                ),
            )
            run_dir = repo_dir / "runs" / "run_1"
            run_dir.mkdir(parents=True, exist_ok=True)
            prompt_bundle = freeze_swarm_prompt_bundle(run_dir=run_dir, loaded=loaded)

            danger_map_provider = SequenceProvider(
                [
                    {
                        "trust_boundaries": ["api"],
                        "risky_sinks": ["sql"],
                        "auth_assumptions": ["cookie"],
                        "hot_paths": ["app/service.py"],
                        "notes": ["watch auth"],
                    }
                ]
            )
            generate_danger_map(
                cwd=repo_dir,
                loaded=loaded,
                provider=danger_map_provider,
                prompt_bundle=prompt_bundle,
            )
            self.assertEqual("low", danger_map_provider.start_calls[0]["reasoning_effort"])

            swarm_digest = run_dir / "derived_context" / "swarm_digest.md"
            shared_manifest = run_dir / "resources" / "shared" / "manifest.md"
            _write(swarm_digest, "# digest\n")
            _write(shared_manifest, "# shared\n")

            sweep_provider = SequenceProvider(
                [
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
                        "summary": "Tight written proof.",
                        "preconditions": ["Reach the vulnerable endpoint."],
                        "repro_steps": [],
                        "citations": ["app/service.py:1"],
                        "notes": [],
                        "filter_reason": "",
                    },
                ]
            )
            run_swarm_sweep(
                cwd=repo_dir,
                loaded=loaded,
                provider=sweep_provider,
                prompt_bundle=prompt_bundle,
                run_dir=run_dir,
                swarm_digest_path=swarm_digest,
                shared_manifest_path=shared_manifest,
                eligible_files=[(repo_dir / "app" / "service.py").resolve()],
            )

            self.assertEqual("high", sweep_provider.start_calls[0]["reasoning_effort"])
            self.assertEqual("low", sweep_provider.start_calls[1]["reasoning_effort"])

    def test_generate_danger_map_passes_configured_rate_limit_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            loaded = self._loaded_config_from_text(
                repo_dir,
                _config_text().replace(
                    '[swarm.prompts]',
                    '[swarm.retries]\nrate_limits = 4\n\n[swarm.prompts]',
                    1,
                ),
            )
            run_dir = repo_dir / "runs" / "run_1"
            run_dir.mkdir(parents=True, exist_ok=True)
            prompt_bundle = freeze_swarm_prompt_bundle(run_dir=run_dir, loaded=loaded)

            with mock.patch(
                "swarm._run_swarm_background_worker",
                return_value=ProviderTurnResult(
                    response_id="bg_1",
                    final_text=json.dumps(
                        {
                            "trust_boundaries": ["api"],
                            "risky_sinks": ["sql"],
                            "auth_assumptions": ["cookie"],
                            "hot_paths": ["app/service.py"],
                            "notes": ["watch auth"],
                        }
                    ),
                    tool_traces=(),
                    status="completed",
                    model="gpt-5.4-mini",
                ),
            ) as worker:
                generate_danger_map(
                    cwd=repo_dir,
                    loaded=loaded,
                    provider=SequenceProvider([]),
                    prompt_bundle=prompt_bundle,
                )

            self.assertEqual(4, worker.call_args.kwargs["rate_limit_max_retries"])

    def test_swarm_passes_configured_parallel_limits_to_seed_and_proof_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            loaded = self._loaded_config_from_text(
                repo_dir,
                _config_text().replace(
                    '[swarm.prompts]',
                    '[swarm.parallelism]\nseed = 3\nproof = 2\n\n[swarm.retries]\nrate_limits = 4\n\n[swarm.prompts]',
                    1,
                ),
            )
            run_dir = repo_dir / "runs" / "run_1"
            run_dir.mkdir(parents=True, exist_ok=True)
            prompt_bundle = freeze_swarm_prompt_bundle(run_dir=run_dir, loaded=loaded)

            swarm_digest = run_dir / "derived_context" / "swarm_digest.md"
            shared_manifest = run_dir / "resources" / "shared" / "manifest.md"
            _write(swarm_digest, "# digest\n")
            _write(shared_manifest, "# shared\n")

            provider = SequenceProvider(
                [
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
                        "summary": "Tight written proof.",
                        "preconditions": ["Reach the vulnerable endpoint."],
                        "repro_steps": [],
                        "citations": ["app/service.py:1"],
                        "notes": [],
                        "filter_reason": "",
                    },
                ]
            )

            with mock.patch("swarm.run_background_swarm_workers", wraps=run_background_swarm_workers) as wrapped:
                result = run_swarm_sweep(
                    cwd=repo_dir,
                    loaded=loaded,
                    provider=provider,
                    prompt_bundle=prompt_bundle,
                    run_dir=run_dir,
                    swarm_digest_path=swarm_digest,
                    shared_manifest_path=shared_manifest,
                    eligible_files=[(repo_dir / "app" / "service.py").resolve()],
                )

            self.assertEqual(1, len(result.proof_results))
            self.assertEqual(2, wrapped.call_count)
            self.assertEqual(3, wrapped.call_args_list[0].kwargs["max_parallel"])
            self.assertEqual(2, wrapped.call_args_list[1].kwargs["max_parallel"])
            self.assertEqual(4, wrapped.call_args_list[0].kwargs["rate_limit_max_retries"])
            self.assertEqual(4, wrapped.call_args_list[1].kwargs["rate_limit_max_retries"])

    def test_swarm_groups_duplicate_seed_findings_into_one_issue_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "app" / "routes.py", "def routes():\n    return 'ok'\n")
            _write(repo_dir / "app" / "db.py", "def load_note():\n    return 'note'\n")
            loaded = self._loaded_config(repo_dir)
            run_dir = repo_dir / "runs" / "run_1"
            run_dir.mkdir(parents=True, exist_ok=True)
            prompt_bundle = freeze_swarm_prompt_bundle(run_dir=run_dir, loaded=loaded)

            swarm_digest = run_dir / "derived_context" / "swarm_digest.md"
            shared_manifest = run_dir / "resources" / "shared" / "manifest.md"
            _write(swarm_digest, "# digest\n")
            _write(shared_manifest, "# shared\n")

            provider = SequenceProvider(
                [
                    {
                        "outcome": "finding",
                        "severity_bucket": "high",
                        "claim": "Cross-user read through unscoped note lookup.",
                        "evidence": ["app/routes.py:1", "app/db.py:1"],
                        "related_files": ["app/db.py"],
                        "notes": [],
                    },
                    {
                        "outcome": "finding",
                        "severity_bucket": "medium",
                        "claim": "Cross-user read through unscoped note lookup helper.",
                        "evidence": ["app/db.py:1"],
                        "related_files": ["app/routes.py"],
                        "notes": [],
                    },
                    {
                        "outcome": "reportable",
                        "proof_state": "executed_proof",
                        "claim": "Cross-user read through unscoped note lookup.",
                        "summary": "The route reads a note by id without owner scoping.",
                        "preconditions": ["Authenticate as any valid user."],
                        "repro_steps": ["Request another user's note id.", "Observe the note body is returned."],
                        "citations": ["app/routes.py:1", "app/db.py:1"],
                        "notes": ["Seed overlap confirmed."],
                        "filter_reason": "",
                    },
                ]
            )

            result = run_swarm_sweep(
                cwd=repo_dir,
                loaded=loaded,
                provider=provider,
                prompt_bundle=prompt_bundle,
                run_dir=run_dir,
                swarm_digest_path=swarm_digest,
                shared_manifest_path=shared_manifest,
                eligible_files=[
                    (repo_dir / "app" / "routes.py").resolve(),
                    (repo_dir / "app" / "db.py").resolve(),
                ],
            )

            self.assertEqual(2, len(result.seed_results))
            self.assertEqual(1, len(result.issue_candidates))
            self.assertEqual({"SEED-001", "SEED-002"}, set(result.issue_candidates[0].seed_ids))
            self.assertEqual(1, len(result.issue_candidates[0].duplicate_seed_ids))
            duplicate_seed_id = result.issue_candidates[0].duplicate_seed_ids[0]
            proof_input = json.loads(provider.start_calls[2]["input_text"])
            self.assertEqual("proof_issue", proof_input["task_type"])
            self.assertEqual("issue:SWM-001", proof_input["lease_key"])
            self.assertTrue((result.proofs_dir / "swm_001.json").exists())
            self.assertTrue((result.proofs_dir / "swm_001.md").exists())

            case_groups = result.case_groups.read_text(encoding="utf-8")
            ranked = result.final_ranked_findings.read_text(encoding="utf-8")
            self.assertIn(f"Duplicate seeds: `{duplicate_seed_id}`", case_groups)
            self.assertIn("Proof state: `executed_proof`", case_groups)
            self.assertIn(f"Related duplicate seeds: `{duplicate_seed_id}`", ranked)
            self.assertIn("Case: `SWM-001`", ranked)

    def test_swarm_does_not_group_seeds_without_bidirectional_target_links(self) -> None:
        issue_candidates = promote_issue_candidates(
            [
                SwarmSeedResult(
                    seed_id="SEED-001",
                    target_file="app/routes.py",
                    outcome="finding",
                    severity_bucket="high",
                    claim="Cross-user read through unscoped note lookup.",
                    evidence=("app/db.py:10",),
                    related_files=("app/db.py",),
                    notes=(),
                ),
                SwarmSeedResult(
                    seed_id="SEED-002",
                    target_file="app/admin.py",
                    outcome="finding",
                    severity_bucket="medium",
                    claim="Cross-user read through unscoped note lookup.",
                    evidence=("app/db.py:25",),
                    related_files=("app/db.py",),
                    notes=(),
                ),
            ]
        )

        self.assertEqual(2, len(issue_candidates))
        self.assertEqual(("SEED-001",), issue_candidates[0].seed_ids)
        self.assertEqual(("SEED-002",), issue_candidates[1].seed_ids)

    def test_swarm_filters_non_reportable_proof_from_ranked_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            loaded = self._loaded_config(repo_dir)
            run_dir = repo_dir / "runs" / "run_1"
            run_dir.mkdir(parents=True, exist_ok=True)
            prompt_bundle = freeze_swarm_prompt_bundle(run_dir=run_dir, loaded=loaded)

            swarm_digest = run_dir / "derived_context" / "swarm_digest.md"
            shared_manifest = run_dir / "resources" / "shared" / "manifest.md"
            _write(swarm_digest, "# digest\n")
            _write(shared_manifest, "# shared\n")

            provider = SequenceProvider(
                [
                    {
                        "outcome": "finding",
                        "severity_bucket": "medium",
                        "claim": "Possible auth bypass.",
                        "evidence": ["app/service.py:1"],
                        "related_files": [],
                        "notes": [],
                    },
                    {
                        "outcome": "not_reportable",
                        "proof_state": "path_grounded",
                        "claim": "Possible auth bypass.",
                        "summary": "The path looks risky but the production preconditions are missing.",
                        "preconditions": ["Debug mode enabled."],
                        "repro_steps": [],
                        "citations": ["app/service.py:1"],
                        "notes": [],
                        "filter_reason": "debug-only branch",
                    },
                ]
            )

            result = run_swarm_sweep(
                cwd=repo_dir,
                loaded=loaded,
                provider=provider,
                prompt_bundle=prompt_bundle,
                run_dir=run_dir,
                swarm_digest_path=swarm_digest,
                shared_manifest_path=shared_manifest,
                eligible_files=[(repo_dir / "app" / "service.py").resolve()],
            )

            ranked = result.final_ranked_findings.read_text(encoding="utf-8")
            summary = result.final_summary.read_text(encoding="utf-8")
            self.assertIn("No findings cleared the proof-stage report bar.", ranked)
            self.assertIn("## Filtered out", ranked)
            self.assertIn("debug-only branch", ranked)
            self.assertIn("- Path-grounded only: `1`", summary)
            self.assertIn("- Findings kept after proof: `0`", summary)

    def test_swarm_respects_explicit_not_reportable_written_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            loaded = self._loaded_config(repo_dir)
            run_dir = repo_dir / "runs" / "run_1"
            run_dir.mkdir(parents=True, exist_ok=True)
            prompt_bundle = freeze_swarm_prompt_bundle(run_dir=run_dir, loaded=loaded)

            swarm_digest = run_dir / "derived_context" / "swarm_digest.md"
            shared_manifest = run_dir / "resources" / "shared" / "manifest.md"
            _write(swarm_digest, "# digest\n")
            _write(shared_manifest, "# shared\n")

            provider = SequenceProvider(
                [
                    {
                        "outcome": "finding",
                        "severity_bucket": "medium",
                        "claim": "Possible auth bypass.",
                        "evidence": ["app/service.py:1"],
                        "related_files": [],
                        "notes": [],
                    },
                    {
                        "outcome": "not_reportable",
                        "proof_state": "written_proof",
                        "claim": "Possible auth bypass.",
                        "summary": "There is a debug-only exploit sketch, not a production issue.",
                        "preconditions": ["Debug mode enabled."],
                        "repro_steps": ["Set DEBUG=true.", "Hit the local-only path."],
                        "citations": ["app/service.py:1"],
                        "notes": [],
                        "filter_reason": "debug-only branch",
                    },
                ]
            )

            result = run_swarm_sweep(
                cwd=repo_dir,
                loaded=loaded,
                provider=provider,
                prompt_bundle=prompt_bundle,
                run_dir=run_dir,
                swarm_digest_path=swarm_digest,
                shared_manifest_path=shared_manifest,
                eligible_files=[(repo_dir / "app" / "service.py").resolve()],
            )

            proof_result = result.proof_results[0]
            ranked = result.final_ranked_findings.read_text(encoding="utf-8")
            summary = result.final_summary.read_text(encoding="utf-8")
            self.assertEqual("not_reportable", proof_result.outcome)
            self.assertEqual("debug-only branch", proof_result.filter_reason)
            self.assertFalse(proof_result.meets_report_bar)
            self.assertIn("No findings cleared the proof-stage report bar.", ranked)
            self.assertIn("debug-only branch", ranked)
            self.assertIn("- Written proofs: `1`", summary)
            self.assertIn("- Findings kept after proof: `0`", summary)

    def test_swarm_filters_contradictory_reportable_written_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            loaded = self._loaded_config(repo_dir)
            run_dir = repo_dir / "runs" / "run_1"
            run_dir.mkdir(parents=True, exist_ok=True)
            prompt_bundle = freeze_swarm_prompt_bundle(run_dir=run_dir, loaded=loaded)

            swarm_digest = run_dir / "derived_context" / "swarm_digest.md"
            shared_manifest = run_dir / "resources" / "shared" / "manifest.md"
            _write(swarm_digest, "# digest\n")
            _write(shared_manifest, "# shared\n")

            provider = SequenceProvider(
                [
                    {
                        "outcome": "finding",
                        "severity_bucket": "medium",
                        "claim": "Possible auth bypass.",
                        "evidence": ["app/service.py:1"],
                        "related_files": [],
                        "notes": [],
                    },
                    {
                        "outcome": "reportable",
                        "proof_state": "written_proof",
                        "claim": "Possible auth bypass.",
                        "summary": "This does not meet the report bar and is only a hardening concern.",
                        "preconditions": ["Debug mode enabled."],
                        "repro_steps": ["Inspect the local-only branch."],
                        "citations": ["app/service.py:1"],
                        "notes": ["Theoretical only."],
                        "filter_reason": "",
                    },
                ]
            )

            result = run_swarm_sweep(
                cwd=repo_dir,
                loaded=loaded,
                provider=provider,
                prompt_bundle=prompt_bundle,
                run_dir=run_dir,
                swarm_digest_path=swarm_digest,
                shared_manifest_path=shared_manifest,
                eligible_files=[(repo_dir / "app" / "service.py").resolve()],
            )

            proof_result = result.proof_results[0]
            ranked = result.final_ranked_findings.read_text(encoding="utf-8")
            summary = result.final_summary.read_text(encoding="utf-8")
            self.assertEqual("not_reportable", proof_result.outcome)
            self.assertEqual("path_grounded", proof_result.proof_state)
            self.assertIn("proof summary contradicts reportable outcome", proof_result.filter_reason)
            self.assertIn("No findings cleared the proof-stage report bar.", ranked)
            self.assertIn("contradicts reportable outcome", ranked)
            self.assertIn("- Proof stage mode: `read-only validation`", summary)

    def test_repo_tools_reject_tracked_symlink_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            outside_file = root / "shared" / "linked.py"
            _write(outside_file, "print('linked')\n")
            (repo_dir / "app").mkdir(parents=True, exist_ok=True)
            (repo_dir / "app" / "link.py").symlink_to(outside_file)
            loaded = self._loaded_config(repo_dir)
            tools = RepoReadOnlyTools(
                cwd=repo_dir,
                scope_include=loaded.effective.scope.include,
                scope_exclude=loaded.effective.scope.exclude,
            )

            git_result = mock.Mock(returncode=0, stdout="app/link.py\n")
            with mock.patch("swarm.subprocess.run", return_value=git_result):
                self.assertEqual([], list_eligible_swarm_files(repo_dir, loaded))
                listing = json.loads(tools.run("list_scope_files", {}))

                self.assertEqual(0, listing["count"])
                with self.assertRaisesRegex(RuntimeError, "outside the configured readable repo scope"):
                    tools.run("read_file", {"path": "app/link.py"})

    def test_repo_tools_allow_only_current_run_staged_shared_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            run_dir = repo_dir / "runs" / "run_1"
            staged_file = run_dir / "resources" / "shared" / "staged" / "01_architecture.md"
            unrelated_run_file = repo_dir / "runs" / "other_run" / "secret.txt"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            _write(staged_file, "shared architecture notes\n")
            _write(unrelated_run_file, "should stay hidden\n")
            loaded = self._loaded_config(repo_dir)
            tools = RepoReadOnlyTools(
                cwd=repo_dir,
                scope_include=loaded.effective.scope.include,
                scope_exclude=loaded.effective.scope.exclude,
                extra_allowed_paths=(staged_file,),
            )

            listing = json.loads(tools.run("list_scope_files", {}))
            self.assertIn("app/service.py", listing["paths"])
            self.assertIn("runs/run_1/resources/shared/staged/01_architecture.md", listing["paths"])
            self.assertNotIn("runs/other_run/secret.txt", listing["paths"])

            staged_data = json.loads(
                tools.run("read_file", {"path": "runs/run_1/resources/shared/staged/01_architecture.md"})
            )
            self.assertIn("shared architecture notes", staged_data["content"])
            with self.assertRaisesRegex(RuntimeError, "outside the configured readable repo scope"):
                tools.run("read_file", {"path": "runs/other_run/secret.txt"})

    def test_repo_tools_read_file_supports_paged_reads_with_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(
                repo_dir / "app" / "service.py",
                "line 1\nline 2\nline 3\nline 4\n",
            )
            loaded = self._loaded_config(repo_dir)
            tools = RepoReadOnlyTools(
                cwd=repo_dir,
                scope_include=loaded.effective.scope.include,
                scope_exclude=loaded.effective.scope.exclude,
            )

            payload = json.loads(
                tools.run(
                    "read_file",
                    {
                        "path": "app/service.py",
                        "start_line": 2,
                        "max_lines": 2,
                    },
                )
            )

            self.assertEqual(2, payload["start_line"])
            self.assertEqual(3, payload["end_line"])
            self.assertTrue(payload["truncated_before"])
            self.assertTrue(payload["truncated_after"])
            self.assertEqual(5, payload["raw_line_count"])
            self.assertEqual(len("line 1\nline 2\nline 3\nline 4\n"), payload["raw_char_count"])
            self.assertIn("2 | line 2", payload["content"])
            self.assertIn("3 | line 3", payload["content"])

    def test_background_scheduler_emits_human_tool_progress_and_writes_trace_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            repo_dir.mkdir(parents=True)
            provider = ContinuationToolProvider()
            trace_path = repo_dir / "runs" / "run_1" / "swarm" / "tool_trace.jsonl"
            progress_events: list[tuple[str, dict[str, object]]] = []
            job = SwarmWorkerJob(
                worker_id="job_tools",
                worker_type="seed_file",
                lease_key="file:app/service.py",
                model="gpt-5.4-mini",
                reasoning_effort="low",
                instructions="seed",
                input_text="seed",
                prompt_cache_key="cache",
                text_format=None,
                tools=(),
            )

            results = run_background_swarm_workers(
                provider=provider,
                jobs=[job],
                tool_executor=lambda name, arguments: "",
                max_parallel=1,
                poll_interval_seconds=0.0,
                stage_name="seed",
                cwd=repo_dir,
                provider_name="openai",
                tool_trace_path=trace_path,
                progress_callback=lambda event_type, data: progress_events.append((event_type, data)),
            )

            self.assertEqual({"job_tools"}, set(results))
            self.assertEqual(1, len(provider.continue_calls))
            self.assertEqual("bg_1", provider.continue_calls[0]["previous_response_id"])

            tool_summaries = [
                str(data["summary"])
                for event_type, data in progress_events
                if event_type == "worker_tool_call_requested"
            ]
            self.assertEqual(2, len(tool_summaries))
            self.assertIn("reading app/service.py lines 1-2 for initial context", tool_summaries)
            self.assertIn("scanning tests/** for nearby clues while inspecting app/service.py", tool_summaries)

            trace_lines = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(2, len(trace_lines))
            self.assertEqual("read_file", trace_lines[0]["tool"])
            self.assertEqual("list_scope_files", trace_lines[1]["tool"])
            self.assertEqual(1, trace_lines[0]["output_meta"]["start_line"])
            self.assertEqual(2, trace_lines[0]["output_meta"]["end_line"])
            self.assertEqual(False, trace_lines[0]["output_meta"]["truncated_after"])

    def test_background_scheduler_serializes_duplicate_lease_keys(self) -> None:
        provider = SequenceProvider(
            [{"ok": 1}, {"ok": 2}, {"ok": 3}]
        )
        jobs = [
            SwarmWorkerJob(
                worker_id="job_a1",
                worker_type="seed_file",
                lease_key="file:app/a.py",
                model="gpt-5.4-mini",
                reasoning_effort="low",
                instructions="seed",
                input_text="a1",
                prompt_cache_key="cache",
                text_format=None,
                tools=(),
            ),
            SwarmWorkerJob(
                worker_id="job_a2",
                worker_type="seed_file",
                lease_key="file:app/a.py",
                model="gpt-5.4-mini",
                reasoning_effort="low",
                instructions="seed",
                input_text="a2",
                prompt_cache_key="cache",
                text_format=None,
                tools=(),
            ),
            SwarmWorkerJob(
                worker_id="job_b1",
                worker_type="seed_file",
                lease_key="file:app/b.py",
                model="gpt-5.4-mini",
                reasoning_effort="low",
                instructions="seed",
                input_text="b1",
                prompt_cache_key="cache",
                text_format=None,
                tools=(),
            ),
        ]

        results = run_background_swarm_workers(
            provider=provider,
            jobs=jobs,
            tool_executor=lambda name, arguments: "",
            max_parallel=2,
            poll_interval_seconds=0.0,
        )

        self.assertEqual(["a1", "b1", "a2"], [call["input_text"] for call in provider.start_calls])
        self.assertEqual({"job_a1", "job_a2", "job_b1"}, set(results))

    def test_background_scheduler_respects_parallel_limit(self) -> None:
        provider = MaxInFlightProvider(
            [{"ok": 1}, {"ok": 2}, {"ok": 3}]
        )
        jobs = [
            SwarmWorkerJob(
                worker_id="job_a",
                worker_type="seed_file",
                lease_key="file:app/a.py",
                model="gpt-5.4-mini",
                reasoning_effort="low",
                instructions="seed",
                input_text="a",
                prompt_cache_key="cache",
                text_format=None,
                tools=(),
            ),
            SwarmWorkerJob(
                worker_id="job_b",
                worker_type="seed_file",
                lease_key="file:app/b.py",
                model="gpt-5.4-mini",
                reasoning_effort="low",
                instructions="seed",
                input_text="b",
                prompt_cache_key="cache",
                text_format=None,
                tools=(),
            ),
            SwarmWorkerJob(
                worker_id="job_c",
                worker_type="seed_file",
                lease_key="file:app/c.py",
                model="gpt-5.4-mini",
                reasoning_effort="low",
                instructions="seed",
                input_text="c",
                prompt_cache_key="cache",
                text_format=None,
                tools=(),
            ),
        ]

        results = run_background_swarm_workers(
            provider=provider,
            jobs=jobs,
            tool_executor=lambda name, arguments: "",
            max_parallel=2,
            poll_interval_seconds=0.0,
        )

        self.assertEqual({"job_a", "job_b", "job_c"}, set(results))
        self.assertEqual(2, provider.max_in_flight)

    def test_background_scheduler_emits_progress_events(self) -> None:
        provider = SequenceProvider([{"ok": True}])
        jobs = [
            SwarmWorkerJob(
                worker_id="job_a",
                worker_type="seed_file",
                lease_key="file:app/a.py",
                model="gpt-5.4-mini",
                reasoning_effort="low",
                instructions="seed",
                input_text="a",
                prompt_cache_key="cache",
                text_format=None,
                tools=(),
            )
        ]
        progress_events: list[tuple[str, dict[str, object]]] = []

        results = run_background_swarm_workers(
            provider=provider,
            jobs=jobs,
            tool_executor=lambda name, arguments: "",
            stage_name="seed",
            max_parallel=1,
            poll_interval_seconds=0.0,
            progress_callback=lambda event_type, data: progress_events.append((event_type, dict(data))),
        )

        self.assertEqual({"job_a"}, set(results))
        self.assertEqual(
            ["stage_started", "worker_started", "worker_completed", "stage_completed"],
            [event_type for event_type, _ in progress_events],
        )
        self.assertEqual("seed", progress_events[0][1]["stage_name"])
        self.assertEqual("app/a.py", progress_events[1][1]["label"])
        self.assertEqual("inspect app/a.py", progress_events[1][1]["action"])
        self.assertGreaterEqual(float(progress_events[2][1]["elapsed_seconds"]), 0.0)
        self.assertEqual(1, progress_events[3][1]["completed_workers"])

    def test_background_scheduler_retries_once(self) -> None:
        provider = RetryProvider()
        jobs = [
            SwarmWorkerJob(
                worker_id="job_retry",
                worker_type="seed_file",
                lease_key="file:app/a.py",
                model="gpt-5.4-mini",
                reasoning_effort="low",
                instructions="seed",
                input_text="retry",
                prompt_cache_key="cache",
                text_format=None,
                tools=(),
            )
        ]

        results = run_background_swarm_workers(
            provider=provider,
            jobs=jobs,
            tool_executor=lambda name, arguments: "",
            max_parallel=1,
            poll_interval_seconds=0.0,
            max_retries=1,
        )

        self.assertEqual({"job_retry"}, set(results))
        self.assertEqual(2, len(provider.start_calls))

    def test_background_scheduler_waits_and_retries_rate_limited_workers(self) -> None:
        provider = RateLimitThenSuccessProvider()
        jobs = [
            SwarmWorkerJob(
                worker_id="job_retry",
                worker_type="seed_file",
                lease_key="file:app/a.py",
                model="gpt-5.4-mini",
                reasoning_effort="low",
                instructions="seed",
                input_text="retry",
                prompt_cache_key="cache",
                text_format=None,
                tools=(),
            )
        ]

        current_time = 10.0
        sleeps: list[float] = []

        def fake_monotonic() -> float:
            return current_time

        def fake_sleep(seconds: float) -> None:
            nonlocal current_time
            sleeps.append(seconds)
            current_time += seconds

        with (
            mock.patch("swarm.time.monotonic", side_effect=fake_monotonic),
            mock.patch("swarm.time.sleep", side_effect=fake_sleep),
        ):
            results = run_background_swarm_workers(
                provider=provider,
                jobs=jobs,
                tool_executor=lambda name, arguments: "",
                max_parallel=1,
                poll_interval_seconds=0.0,
                max_retries=0,
                rate_limit_max_retries=1,
            )

        self.assertEqual({"job_retry"}, set(results))
        self.assertEqual(2, len(provider.start_calls))
        self.assertTrue(any(seconds >= 2.5 for seconds in sleeps))

    def test_background_scheduler_waits_and_retries_millisecond_rate_limited_workers(self) -> None:
        provider = RateLimitMillisThenSuccessProvider()
        jobs = [
            SwarmWorkerJob(
                worker_id="job_retry",
                worker_type="seed_file",
                lease_key="file:app/a.py",
                model="gpt-5.4-mini",
                reasoning_effort="low",
                instructions="seed",
                input_text="retry",
                prompt_cache_key="cache",
                text_format=None,
                tools=(),
            )
        ]

        current_time = 10.0
        sleeps: list[float] = []

        def fake_monotonic() -> float:
            return current_time

        def fake_sleep(seconds: float) -> None:
            nonlocal current_time
            sleeps.append(seconds)
            current_time += seconds

        with (
            mock.patch("swarm.time.monotonic", side_effect=fake_monotonic),
            mock.patch("swarm.time.sleep", side_effect=fake_sleep),
        ):
            results = run_background_swarm_workers(
                provider=provider,
                jobs=jobs,
                tool_executor=lambda name, arguments: "",
                max_parallel=1,
                poll_interval_seconds=0.0,
                max_retries=0,
                rate_limit_max_retries=1,
            )

        self.assertEqual({"job_retry"}, set(results))
        self.assertEqual(2, len(provider.start_calls))
        self.assertTrue(any(seconds >= 0.93 for seconds in sleeps), sleeps)

    def test_background_scheduler_bootstraps_at_one_worker_then_opens_parallelism(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            repo_dir.mkdir(parents=True)
            save_learned_model_limit(
                cwd=repo_dir,
                provider="openai",
                model="gpt-5.4-mini",
                learned_tpm_limit=200,
                headroom_fraction=0.85,
                observed_peak_input_tokens={"seed_file": 30},
            )
            provider = MaxInFlightUsageProvider(usage_tokens=20)
            jobs = [
                SwarmWorkerJob(
                    worker_id="job_a",
                    worker_type="seed_file",
                    lease_key="file:app/a.py",
                    model="gpt-5.4-mini",
                    reasoning_effort="low",
                    instructions="seed",
                    input_text="a",
                    prompt_cache_key="cache",
                    text_format=None,
                    tools=(),
                ),
                SwarmWorkerJob(
                    worker_id="job_b",
                    worker_type="seed_file",
                    lease_key="file:app/b.py",
                    model="gpt-5.4-mini",
                    reasoning_effort="low",
                    instructions="seed",
                    input_text="b",
                    prompt_cache_key="cache",
                    text_format=None,
                    tools=(),
                ),
                SwarmWorkerJob(
                    worker_id="job_c",
                    worker_type="seed_file",
                    lease_key="file:app/c.py",
                    model="gpt-5.4-mini",
                    reasoning_effort="low",
                    instructions="seed",
                    input_text="c",
                    prompt_cache_key="cache",
                    text_format=None,
                    tools=(),
                ),
            ]

            results = run_background_swarm_workers(
                provider=provider,
                jobs=jobs,
                tool_executor=lambda name, arguments: "",
                max_parallel=2,
                poll_interval_seconds=0.0,
                cwd=repo_dir,
                provider_name="openai",
            )

            self.assertEqual({"job_a", "job_b", "job_c"}, set(results))
            self.assertEqual(2, provider.max_in_flight)
            self.assertEqual([False, True, True], provider.start_usage_emitted_flags)

    def test_background_scheduler_blocks_launch_when_window_exceeds_headroom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            repo_dir.mkdir(parents=True)
            save_learned_model_limit(
                cwd=repo_dir,
                provider="openai",
                model="gpt-5.4-mini",
                learned_tpm_limit=100,
                headroom_fraction=0.85,
                observed_peak_input_tokens={"seed_file": 60},
            )
            provider = MaxInFlightUsageProvider(usage_tokens=60)
            jobs = [
                SwarmWorkerJob(
                    worker_id="job_a",
                    worker_type="seed_file",
                    lease_key="file:app/a.py",
                    model="gpt-5.4-mini",
                    reasoning_effort="low",
                    instructions="seed",
                    input_text="a",
                    prompt_cache_key="cache",
                    text_format=None,
                    tools=(),
                ),
                SwarmWorkerJob(
                    worker_id="job_b",
                    worker_type="seed_file",
                    lease_key="file:app/b.py",
                    model="gpt-5.4-mini",
                    reasoning_effort="low",
                    instructions="seed",
                    input_text="b",
                    prompt_cache_key="cache",
                    text_format=None,
                    tools=(),
                ),
                SwarmWorkerJob(
                    worker_id="job_c",
                    worker_type="seed_file",
                    lease_key="file:app/c.py",
                    model="gpt-5.4-mini",
                    reasoning_effort="low",
                    instructions="seed",
                    input_text="c",
                    prompt_cache_key="cache",
                    text_format=None,
                    tools=(),
                ),
            ]
            fake_now = [0.0]
            sleeps: list[float] = []

            def fake_monotonic() -> float:
                return fake_now[0]

            def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)
                fake_now[0] += seconds

            with (
                mock.patch("swarm.time.monotonic", side_effect=fake_monotonic),
                mock.patch("swarm.time.sleep", side_effect=fake_sleep),
            ):
                results = run_background_swarm_workers(
                    provider=provider,
                    jobs=jobs,
                    tool_executor=lambda name, arguments: "",
                    max_parallel=2,
                    poll_interval_seconds=0.0,
                    cwd=repo_dir,
                    provider_name="openai",
                )

            self.assertEqual({"job_a", "job_b", "job_c"}, set(results))
            self.assertEqual(1, provider.max_in_flight)
            self.assertEqual([False, True, True], provider.start_usage_emitted_flags)
            self.assertTrue(any(seconds >= 60.0 for seconds in sleeps), sleeps)

    def test_background_scheduler_reuses_persisted_learned_limit_on_later_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            repo_dir.mkdir(parents=True)
            first_provider = RateLimitThenUsageProvider(
                usage_tokens=30,
                message=(
                    "ResponseError(code='rate_limit_exceeded', message='Rate limit reached for "
                    "gpt-5.4-mini on tokens per min (TPM): Limit 200000, Used 188404, Requested "
                    "19721. Please try again in 10ms.')"
                ),
            )
            first_job = SwarmWorkerJob(
                worker_id="job_a",
                worker_type="seed_file",
                lease_key="file:app/a.py",
                model="gpt-5.4-mini",
                reasoning_effort="low",
                instructions="seed",
                input_text="a",
                prompt_cache_key="cache",
                text_format=None,
                tools=(),
            )

            results = run_background_swarm_workers(
                provider=first_provider,
                jobs=[first_job],
                tool_executor=lambda name, arguments: "",
                max_parallel=1,
                poll_interval_seconds=0.0,
                rate_limit_max_retries=1,
                cwd=repo_dir,
                provider_name="openai",
            )

            self.assertEqual({"job_a"}, set(results))
            learned = load_learned_model_limit(
                cwd=repo_dir,
                provider="openai",
                model="gpt-5.4-mini",
            )
            self.assertIsNotNone(learned)
            self.assertEqual(200000, learned.learned_tpm_limit)
            self.assertEqual(30, learned.observed_peak_input_tokens["seed_file"])

            second_provider = MaxInFlightUsageProvider(usage_tokens=30)
            second_jobs = [
                SwarmWorkerJob(
                    worker_id="job_b",
                    worker_type="seed_file",
                    lease_key="file:app/b.py",
                    model="gpt-5.4-mini",
                    reasoning_effort="low",
                    instructions="seed",
                    input_text="b",
                    prompt_cache_key="cache",
                    text_format=None,
                    tools=(),
                ),
                SwarmWorkerJob(
                    worker_id="job_c",
                    worker_type="seed_file",
                    lease_key="file:app/c.py",
                    model="gpt-5.4-mini",
                    reasoning_effort="low",
                    instructions="seed",
                    input_text="c",
                    prompt_cache_key="cache",
                    text_format=None,
                    tools=(),
                ),
                SwarmWorkerJob(
                    worker_id="job_d",
                    worker_type="seed_file",
                    lease_key="file:app/d.py",
                    model="gpt-5.4-mini",
                    reasoning_effort="low",
                    instructions="seed",
                    input_text="d",
                    prompt_cache_key="cache",
                    text_format=None,
                    tools=(),
                ),
            ]

            second_results = run_background_swarm_workers(
                provider=second_provider,
                jobs=second_jobs,
                tool_executor=lambda name, arguments: "",
                max_parallel=2,
                poll_interval_seconds=0.0,
                cwd=repo_dir,
                provider_name="openai",
            )

            self.assertEqual({"job_b", "job_c", "job_d"}, set(second_results))
            self.assertEqual(2, second_provider.max_in_flight)
            self.assertEqual([False, True, True], second_provider.start_usage_emitted_flags)

    def test_background_scheduler_aborts_stage_after_exhausted_rate_limits_and_drains_active_workers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            repo_dir.mkdir(parents=True)
            save_learned_model_limit(
                cwd=repo_dir,
                provider="openai",
                model="gpt-5.4-mini",
                learned_tpm_limit=200,
                headroom_fraction=0.85,
                observed_peak_input_tokens={"seed_file": 20},
            )
            provider = DrainAfterAbortProvider(
                failure_message=(
                    "ResponseError(code='rate_limit_exceeded', message='Rate limit reached for "
                    "gpt-5.4-mini on tokens per min (TPM): Limit 200000, Used 184418, Requested "
                    "43593. Please try again in 8.403s.')"
                ),
            )
            metrics = SwarmRunMetrics(path=repo_dir / "runs" / "run_1" / "swarm" / "usage_summary.json")
            metrics.write()
            completed_workers: list[str] = []
            jobs = [
                SwarmWorkerJob(
                    worker_id="job_a",
                    worker_type="seed_file",
                    lease_key="file:app/a.py",
                    model="gpt-5.4-mini",
                    reasoning_effort="low",
                    instructions="seed",
                    input_text="a",
                    prompt_cache_key="cache",
                    text_format=None,
                    tools=(),
                ),
                SwarmWorkerJob(
                    worker_id="job_b",
                    worker_type="seed_file",
                    lease_key="file:app/b.py",
                    model="gpt-5.4-mini",
                    reasoning_effort="low",
                    instructions="seed",
                    input_text="b",
                    prompt_cache_key="cache",
                    text_format=None,
                    tools=(),
                ),
                SwarmWorkerJob(
                    worker_id="job_c",
                    worker_type="seed_file",
                    lease_key="file:app/c.py",
                    model="gpt-5.4-mini",
                    reasoning_effort="low",
                    instructions="seed",
                    input_text="c",
                    prompt_cache_key="cache",
                    text_format=None,
                    tools=(),
                ),
            ]

            with self.assertRaises(SwarmStageAbort) as exc_info:
                run_background_swarm_workers(
                    provider=provider,
                    jobs=jobs,
                    tool_executor=lambda name, arguments: "",
                    max_parallel=2,
                    poll_interval_seconds=0.0,
                    rate_limit_max_retries=0,
                    on_worker_completed=lambda job, result: completed_workers.append(job.worker_id),
                    metrics_tracker=metrics,
                    stage_name="seed",
                    cwd=repo_dir,
                    provider_name="openai",
                )

            self.assertEqual(["job_a"], completed_workers)
            self.assertEqual(("job_c",), exc_info.exception.skipped_worker_ids)
            self.assertEqual(["a", "b"], [call["input_text"] for call in provider.start_calls])
            self.assertEqual([], provider.cancelled)
            summary = json.loads(metrics.path.read_text(encoding="utf-8"))
            self.assertEqual(1, summary["stages"]["seed"]["skipped_workers"])
            self.assertTrue(summary["stages"]["seed"]["aborted"])
            self.assertTrue(summary["limiter"]["stage_degraded_after_rate_limit"])
            self.assertEqual(1, summary["limiter"]["current_stage_parallel_ceiling"])

    def test_background_scheduler_cancels_active_workers_after_non_rate_limit_failure(self) -> None:
        provider = RunningThenFailureProvider()
        jobs = [
            SwarmWorkerJob(
                worker_id="job_running",
                worker_type="seed_file",
                lease_key="file:app/a.py",
                model="gpt-5.4-mini",
                reasoning_effort="low",
                instructions="seed",
                input_text="a",
                prompt_cache_key="cache",
                text_format=None,
                tools=(),
            ),
            SwarmWorkerJob(
                worker_id="job_fail",
                worker_type="seed_file",
                lease_key="file:app/b.py",
                model="gpt-5.4-mini",
                reasoning_effort="low",
                instructions="seed",
                input_text="b",
                prompt_cache_key="cache",
                text_format=None,
                tools=(),
            ),
        ]

        with self.assertRaisesRegex(RuntimeError, "job_fail"):
            run_background_swarm_workers(
                provider=provider,
                jobs=jobs,
                tool_executor=lambda name, arguments: "",
                max_parallel=2,
                poll_interval_seconds=0.0,
                max_retries=0,
            )

        self.assertEqual(["bg_1"], provider.cancelled)

    def test_swarm_persists_completed_seed_artifacts_before_later_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "app" / "a.py", "print('a')\n")
            _write(repo_dir / "app" / "b.py", "print('b')\n")
            loaded = self._loaded_config(repo_dir)
            run_dir = repo_dir / "runs" / "run_1"
            run_dir.mkdir(parents=True, exist_ok=True)
            prompt_bundle = freeze_swarm_prompt_bundle(run_dir=run_dir, loaded=loaded)

            swarm_digest = run_dir / "derived_context" / "swarm_digest.md"
            shared_manifest = run_dir / "resources" / "shared" / "manifest.md"
            _write(swarm_digest, "# digest\n")
            _write(shared_manifest, "# shared\n")

            provider = PartialSeedFailureProvider()

            with (
                self.assertRaisesRegex(RuntimeError, "Swarm worker failure"),
                mock.patch("swarm.DEFAULT_SWARM_MAX_RETRIES", 0),
            ):
                run_swarm_sweep(
                    cwd=repo_dir,
                    loaded=loaded,
                    provider=provider,
                    prompt_bundle=prompt_bundle,
                    run_dir=run_dir,
                    swarm_digest_path=swarm_digest,
                    shared_manifest_path=shared_manifest,
                    eligible_files=[
                        (repo_dir / "app" / "a.py").resolve(),
                        (repo_dir / "app" / "b.py").resolve(),
                    ],
                )

            self.assertTrue((run_dir / "swarm" / "seeds" / "seed_001.json").exists())
            self.assertTrue((run_dir / "swarm" / "seeds" / "seed_001.md").exists())
            usage_summary = json.loads((run_dir / "swarm" / "usage_summary.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", usage_summary["status"])
            self.assertEqual("seed", usage_summary["failure_stage"])

    def test_swarm_seed_stage_abort_writes_partial_summary_and_skips_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "app" / "a.py", "print('a')\n")
            _write(repo_dir / "app" / "b.py", "print('b')\n")
            _write(repo_dir / "app" / "c.py", "print('c')\n")
            loaded = self._loaded_config(repo_dir)
            save_learned_model_limit(
                cwd=repo_dir,
                provider="openai",
                model="gpt-5.4-mini",
                learned_tpm_limit=200,
                headroom_fraction=0.85,
                observed_peak_input_tokens={"seed_file": 20},
            )
            run_dir = repo_dir / "runs" / "run_1"
            run_dir.mkdir(parents=True, exist_ok=True)
            prompt_bundle = freeze_swarm_prompt_bundle(run_dir=run_dir, loaded=loaded)

            swarm_digest = run_dir / "derived_context" / "swarm_digest.md"
            shared_manifest = run_dir / "resources" / "shared" / "manifest.md"
            _write(swarm_digest, "# digest\n")
            _write(shared_manifest, "# shared\n")

            provider = SeedStageAbortProvider(
                failure_message=(
                    "ResponseError(code='rate_limit_exceeded', message='Rate limit reached for "
                    "gpt-5.4-mini on tokens per min (TPM): Limit 200000, Used 184418, Requested "
                    "43593. Please try again in 1ms.')"
                )
            )

            with self.assertRaises(SwarmStageAbort):
                run_swarm_sweep(
                    cwd=repo_dir,
                    loaded=loaded,
                    provider=provider,
                    prompt_bundle=prompt_bundle,
                    run_dir=run_dir,
                    swarm_digest_path=swarm_digest,
                    shared_manifest_path=shared_manifest,
                    eligible_files=[
                        (repo_dir / "app" / "a.py").resolve(),
                        (repo_dir / "app" / "b.py").resolve(),
                        (repo_dir / "app" / "c.py").resolve(),
                    ],
                )

            partial_summary = run_dir / "swarm" / "reports" / "partial_summary.md"
            self.assertTrue(partial_summary.exists())
            partial_text = partial_summary.read_text(encoding="utf-8")
            self.assertIn("Stage aborted: `seed`", partial_text)
            self.assertIn("Completed workers: `1`", partial_text)
            self.assertIn("Skipped workers: `1`", partial_text)
            self.assertTrue((run_dir / "swarm" / "reports" / "seed_ledger.md").exists())
            self.assertFalse((run_dir / "swarm" / "reports" / "final_summary.md").exists())
            self.assertFalse((run_dir / "swarm" / "proofs" / "swm_001.json").exists())

    def test_swarm_persists_usage_summary_with_worker_timings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            loaded = self._loaded_config(repo_dir)
            run_dir = repo_dir / "runs" / "run_1"
            run_dir.mkdir(parents=True, exist_ok=True)
            prompt_bundle = freeze_swarm_prompt_bundle(run_dir=run_dir, loaded=loaded)

            swarm_digest = run_dir / "derived_context" / "swarm_digest.md"
            shared_manifest = run_dir / "resources" / "shared" / "manifest.md"
            _write(swarm_digest, "# digest\n")
            _write(shared_manifest, "# shared\n")

            provider = UsageEventProvider(
                [
                    (
                        {
                            "outcome": "finding",
                            "severity_bucket": "medium",
                            "claim": "seed claim",
                            "evidence": ["app/service.py:1"],
                            "related_files": [],
                            "notes": ["seed note"],
                        },
                        {
                            "input_tokens": 120,
                            "output_tokens": 30,
                            "total_tokens": 150,
                            "cached_input_tokens": 20,
                            "reasoning_output_tokens": 10,
                        },
                    ),
                    (
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
                        {
                            "input_tokens": 240,
                            "output_tokens": 60,
                            "total_tokens": 300,
                            "cached_input_tokens": 40,
                            "reasoning_output_tokens": 25,
                        },
                    ),
                ]
            )

            result = run_swarm_sweep(
                cwd=repo_dir,
                loaded=loaded,
                provider=provider,
                prompt_bundle=prompt_bundle,
                run_dir=run_dir,
                swarm_digest_path=swarm_digest,
                shared_manifest_path=shared_manifest,
                eligible_files=[(repo_dir / "app" / "service.py").resolve()],
            )

            self.assertEqual(run_dir / "swarm" / "usage_summary.json", result.usage_summary)
            summary = json.loads(result.usage_summary.read_text(encoding="utf-8"))
            self.assertEqual("completed", summary["status"])
            self.assertEqual(2, summary["totals"]["responses"])
            self.assertEqual(450, summary["totals"]["total_tokens"])
            self.assertIn("limiter", summary)
            self.assertIn("seed", summary["stages"])
            self.assertIn("proof", summary["stages"])
            self.assertEqual(1, summary["stages"]["seed"]["completed_workers"])
            self.assertEqual(1, summary["stages"]["proof"]["completed_workers"])
            self.assertIn("avg_input_tpm", summary["limiter"])
            self.assertIn("top_token_workers", summary["limiter"])
            self.assertIn("current_stage_parallel_ceiling", summary["limiter"])
            self.assertGreater(summary["wall_clock_seconds"], 0.0)
            self.assertGreater(summary["stages"]["seed"]["elapsed_seconds"], 0.0)

            seed_worker = next(worker for worker in summary["workers"] if worker["worker_id"] == "SEED-001")
            proof_worker = next(worker for worker in summary["workers"] if worker["worker_id"] == "SWM-001")
            self.assertEqual("completed", seed_worker["status"])
            self.assertEqual(150, seed_worker["totals"]["total_tokens"])
            self.assertGreater(seed_worker["elapsed_seconds"], 0.0)
            self.assertEqual("completed", proof_worker["status"])
            self.assertEqual(300, proof_worker["totals"]["total_tokens"])
            self.assertGreater(proof_worker["elapsed_seconds"], 0.0)

    def test_swarm_worker_failure_captures_invalid_payload_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            loaded = self._loaded_config(repo_dir)
            run_dir = repo_dir / "runs" / "run_1"
            run_dir.mkdir(parents=True, exist_ok=True)
            prompt_bundle = freeze_swarm_prompt_bundle(run_dir=run_dir, loaded=loaded)

            swarm_digest = run_dir / "derived_context" / "swarm_digest.md"
            shared_manifest = run_dir / "resources" / "shared" / "manifest.md"
            staged_doc = run_dir / "resources" / "shared" / "staged" / "01_architecture.md"
            _write(swarm_digest, "# digest\n")
            _write(shared_manifest, "# shared\n")
            _write(staged_doc, "shared doc\n")

            provider = SequenceProvider(
                [
                    {
                        "outcome": "finding",
                        "severity_bucket": "medium",
                        "claim": "seed claim",
                        "evidence": ["app/service.py:1"],
                        "related_files": [],
                    }
                ]
            )

            with self.assertRaises(SwarmWorkerFailure) as ctx:
                run_swarm_sweep(
                    cwd=repo_dir,
                    loaded=loaded,
                    provider=provider,
                    prompt_bundle=prompt_bundle,
                    run_dir=run_dir,
                    swarm_digest_path=swarm_digest,
                    shared_manifest_path=shared_manifest,
                    eligible_files=[(repo_dir / "app" / "service.py").resolve()],
                )

            failure = ctx.exception.primary_diagnostic
            self.assertIsNotNone(failure)
            self.assertEqual("seed", failure.stage)
            self.assertEqual("SEED-001", failure.worker_id)
            self.assertIn("missing keys: notes", failure.failure_message)
            self.assertIn('"claim": "seed claim"', failure.raw_final_text)

    def test_generate_danger_map_rejects_missing_structured_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "app" / "service.py", "print('hello')\n")
            loaded = self._loaded_config(repo_dir)
            run_dir = repo_dir / "runs" / "run_1"
            run_dir.mkdir(parents=True, exist_ok=True)
            prompt_bundle = freeze_swarm_prompt_bundle(run_dir=run_dir, loaded=loaded)

            provider = SequenceProvider(
                [
                    {
                        "trust_boundaries": ["api"],
                        "risky_sinks": ["sql"],
                        "auth_assumptions": ["cookie"],
                        "hot_paths": ["app/service.py"],
                    }
                ]
            )

            with self.assertRaisesRegex(RuntimeError, "missing keys: notes"):
                generate_danger_map(
                    cwd=repo_dir,
                    loaded=loaded,
                    provider=provider,
                    prompt_bundle=prompt_bundle,
                )
            self.assertEqual("high", provider.start_calls[0]["reasoning_effort"])


if __name__ == "__main__":
    unittest.main()

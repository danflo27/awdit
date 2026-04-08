from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from config import load_effective_config
from provider_openai import BackgroundPollResult, ProviderBackgroundHandle, ProviderTurnResult
from swarm import (
    RepoReadOnlyTools,
    SwarmSeedResult,
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

    [swarm]
    sweep_model = "gpt-5.4-mini"
    proof_model = "gpt-5.4"
    eligible_file_profile = "code_config_tests"
    token_budget = 120000
    allow_no_limit = true

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
                    'rate_limit_max_retries = 4\n\n[swarm.prompts]',
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
                    'seed_max_parallel = 3\nproof_max_parallel = 2\nrate_limit_max_retries = 4\n\n[swarm.prompts]',
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
            self.assertEqual(("SEED-001", "SEED-002"), result.issue_candidates[0].seed_ids)
            self.assertEqual(("SEED-002",), result.issue_candidates[0].duplicate_seed_ids)
            proof_input = json.loads(provider.start_calls[2]["input_text"])
            self.assertEqual("proof_issue", proof_input["task_type"])
            self.assertEqual("issue:SWM-001", proof_input["lease_key"])
            self.assertTrue((result.proofs_dir / "swm_001.json").exists())
            self.assertTrue((result.proofs_dir / "swm_001.md").exists())

            case_groups = result.case_groups.read_text(encoding="utf-8")
            ranked = result.final_ranked_findings.read_text(encoding="utf-8")
            self.assertIn("Duplicate seeds: `SEED-002`", case_groups)
            self.assertIn("Proof state: `executed_proof`", case_groups)
            self.assertIn("Related duplicate seeds: `SEED-002`", ranked)
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

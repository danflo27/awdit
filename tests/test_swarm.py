from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from config import load_effective_config
from provider_openai import BackgroundPollResult, ProviderBackgroundHandle
from swarm import (
    SwarmWorkerJob,
    freeze_swarm_prompt_bundle,
    generate_danger_map,
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
    (prompt_dir / "swarm_seed.md").write_text("# frozen seed prompt\n", encoding="utf-8")
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


class SwarmTests(unittest.TestCase):
    def _loaded_config(self, repo_dir: Path):
        config_dir = repo_dir / "config"
        _write_prompt_tree(config_dir)
        _write(config_dir / "config.toml", _config_text())
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
            self.assertEqual("# frozen seed prompt\n", provider.start_calls[0]["instructions"])
            self.assertEqual("# frozen proof prompt\n", provider.start_calls[1]["instructions"])
            self.assertEqual(prompt_bundle.seed.prompt_cache_key, provider.start_calls[0]["prompt_cache_key"])
            self.assertEqual(prompt_bundle.proof.prompt_cache_key, provider.start_calls[1]["prompt_cache_key"])
            self.assertIsNone(provider.start_calls[0]["previous_response_id"])
            self.assertIsNone(provider.start_calls[1]["previous_response_id"])

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


if __name__ == "__main__":
    unittest.main()

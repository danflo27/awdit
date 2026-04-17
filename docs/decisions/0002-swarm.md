# 0002. Swarm: one-agent-per-file adversarial sweep, two-stage seed → proof

- **Status:** Accepted — 2026-04-17
- **Applies to:** `awdit swarm`
- **Related:** [0001-review-pipeline.md](0001-review-pipeline.md), [0003-command-split-and-workflows.md](0003-command-split-and-workflows.md), [0004-scope-and-file-selection.md](0004-scope-and-file-selection.md), [0005-storage-and-artifacts.md](0005-storage-and-artifacts.md)
- **Supersedes:** [archive/2026-04-17-repo-wide-black-hat-auditing.md](../archive/2026-04-17-repo-wide-black-hat-auditing.md)

## Context

`awdit review` is designed around robustness — competing agents, bounded debate, and human-in-the-loop truth review. It is deliberately expensive. There is a separate need for a broader, cheaper repo-wide offensive sweep that hunts for plausibly-exploitable bugs across an entire codebase without the ceremony of the review pipeline.

`awdit swarm` fills that need. It is intentionally simpler than the review pipeline and does not try to mirror hunter/skeptic/referee/solver under the hood.

## Decision

### What swarm is

- A separate top-level command (`awdit swarm`). Not a flag on `awdit review`. Not the default face of the product.
- A read-only, repo-wide, black-hat-flavored sweep. One file worker per eligible file. Each worker is adversarially primed to find at most one strongest seed finding.
- Quiet UX: launch, wait, read one ranked report.
- Local-only. See [0003-command-split-and-workflows.md](0003-command-split-and-workflows.md) for rationale.

### Two-stage design

**Sweep stage**
- One worker per eligible file.
- Each worker receives: its seed file, the compact swarm digest (derived from danger map + shared resources), the shared resource manifest, and explicit instructions to produce at most one strongest seed finding.
- Workers may inspect other repo files read-only when needed for context or proof, and must cite exact file paths and line references.
- Workers cannot see or respond to another worker's output. No worker-to-worker debate.
- There are no separate neighbor-expansion workers; follow-up work stays attached to the seed that triggered it.

**Proof stage**
- Only surviving seeds enter proof. Seeds are clustered conservatively first; duplicate seeds group under shared case IDs (`SWM-###`), but the ranked unit remains the seed finding, not the merged case.
- Proof uses a stronger pass than the sweep.
- Proof workers may inspect nearby code and related files but remain attached to one seed.
- Each proof produces one of four states on the proof ladder: `hypothesized`, `path_grounded`, `written_proof`, `executed_proof`.

### Final report bar

- If executable proof is feasible, the finding must include executable repro steps.
- If executable proof is not feasible, the finding must include a tight written exploit proof with exact preconditions and citations.
- Findings that remain merely interesting or suspicious are filtered out of the final ranked report (they remain in the raw artifacts).
- Ranking order: exploitability first, then impact and confidence.

### Preflight contract

Before launch, the CLI shows a single confirm screen with:
- the eligible file count
- the token budget, or that the run is explicitly no-limit
- the configured seed and proof parallelism limits
- the configured rate-limit retry count
- the selected sweep and proof models
- the danger-map path
- the shared resource manifest path

### Danger-map requirement

`awdit swarm` requires a repo danger map before launch. If none exists for the repo, swarm generates it. The operator may accept, edit-and-regenerate, or regenerate-without-guidance. Danger-map lifecycle details live with repo-memory (see [0005-storage-and-artifacts.md](0005-storage-and-artifacts.md)).

### Configuration surface

`awdit swarm` reads the shared config sections (`active_provider`, `providers.*`, `scope`, `repo_memory`, `resources.shared`) plus the `[swarm]` block:

- `[swarm.mode].preset` — `safe` (default) | `balanced` | `fast`
- `[swarm.models].sweep` / `.proof` — any model listed in `providers.<active>.allowed_models`
- `[swarm.budget].tokens` / `.mode` — token budget + `enforced` | `advisory`
- `[swarm.parallelism].seed` / `.proof`
- `[swarm.retries].rate_limits`
- `[swarm.reasoning].danger_map` / `.seed` / `.proof` — `low` | `medium` | `high`
- `[swarm.prompts].danger_map` / `.seed` / `.proof` — prompt file paths

File selection is governed by [0004-scope-and-file-selection.md](0004-scope-and-file-selection.md). Swarm no longer has its own `[swarm.files]` profile — the shared scope rules are the single source of truth.

### Worker artifacts and run layout

See [0005-storage-and-artifacts.md](0005-storage-and-artifacts.md) for the full `runs/<run_id>/swarm/` artifact tree. The key user-facing object is `swarm/reports/final_ranked_findings.md`; everything else exists to preserve traceability, proof, and grouping logic behind that report.

## Consequences

- **Different operator contract than review.** Review is interactive and multi-stage; swarm is launch-and-wait. The product keeps two distinct mental models.
- **Coarse duplicate handling.** Clustering is conservative by design — seeds remain the ranked unit so file-level provenance stays legible in the report. The cost is occasional visible duplication of closely-related seeds.
- **Stronger proof bar than review's skeptic+referee gauntlet.** Swarm compensates for the lack of competing-agent debate by holding the final report to an executable-or-written proof requirement.
- **Coexistence with review.** The two commands share `scope`, `resources.shared`, `repo_memory`, `providers`, and data-root layout. They deliberately do not share their internal pipelines.

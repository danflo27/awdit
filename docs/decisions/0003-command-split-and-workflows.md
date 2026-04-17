# 0003. Command split: review runs locally or in CI, swarm runs locally only

- **Status:** Accepted — 2026-04-17
- **Applies to:** `awdit review`, `awdit swarm`, GitHub Actions integration
- **Related:** [0001-review-pipeline.md](0001-review-pipeline.md), [0002-swarm.md](0002-swarm.md), [0004-scope-and-file-selection.md](0004-scope-and-file-selection.md), [roadmap/review-ci-workflow.md](../roadmap/review-ci-workflow.md)
- **Supersedes:** the CI-workflow sections of [archive/2026-04-17-repo-wide-black-hat-auditing.md](../archive/2026-04-17-repo-wide-black-hat-auditing.md) and the bulk of [archive/2026-04-17-swarm-pr-workflow-todo.md](../archive/2026-04-17-swarm-pr-workflow-todo.md)

## Context

awdit ships two top-level audit commands (`review` and `swarm`) with meaningfully different internal pipelines — see 0001 and 0002. Earlier docs planned a reusable GitHub Actions workflow around `awdit swarm` that would run on every PR. That plan assumed swarm was the right CI primitive. It isn't: review is the more robust of the two, is explicitly designed to target either an entire repo or a specific PR, and is the better fit for the PR-comment / workflow-artifact shape that CI wants.

This ADR locks the command split and the workflow story.

## Decision

### Commands and where they run

| Command | Target shapes | Local | CI (GitHub Actions) |
|---|---|---|---|
| `awdit review` | entire repo, or a specific PR (plus the other `feature review` target shapes — diff-against-main, single commit, explicit file list, pasted text) | yes | yes |
| `awdit swarm` | entire repo, minus scope excludes | yes | no (intentional — see rationale) |

### Interactive vs. non-interactive

- `awdit review` and `awdit swarm` both run as interactive CLI wizards by default when a TTY is attached.
- `awdit review` needs a non-interactive `--ci` mode to run inside a GitHub Actions workflow. Mechanics carry over from the old swarm PR-workflow TODO: `--ci` auto-accepts danger-map and shared-resource prompts, skips the launch-confirm screen, and bypasses any config-override menu. See [roadmap/review-ci-workflow.md](../roadmap/review-ci-workflow.md).
- `awdit swarm` does not need a `--ci` mode today. If we ever enable swarm-in-CI, this ADR is superseded.

### Trusted config injection

Both commands already accept `--config PATH` and `--env-file PATH` so a reusable workflow can load config and secrets from outside the analyzed repo rather than trusting whatever is in the target repo's working tree. The standard CI shape is:

```bash
uv run --project /path/to/awdit awdit review \
  --ci \
  --config /path/to/awdit/config/config.toml \
  --env-file /path/to/awdit/.env \
  [--pr <PR-NUMBER>]
```

`--pr` on `awdit review` is **new work** — see [roadmap/review-ci-workflow.md](../roadmap/review-ci-workflow.md) for the targeting contract.

### Rationale

- **Review is more robust.** Competing agents + bounded debate + merged referee report handle noisy model behavior better than swarm's one-shot-per-file sweep. CI runs benefit most from that robustness because reviewers will rely on the comment without re-running the tool.
- **Swarm-in-CI is low-priority someday-work.** Swarm's launch-and-wait operator model is a poor fit for CI latency budgets and the PR-comment interaction model. If we reconsider, it's a separate ADR.
- **Local-only for swarm reduces surface area.** No `--ci` mode, no sticky-comment logic, no fork-PR secret concerns — fewer moving parts while the tool is early.

### Trust boundaries

- Fork PRs are skipped by default in the reusable workflow; running fork-controlled code with the adopting repo's `OPENAI_API_KEY` is unsafe.
- `.env` files are never used in CI. Secrets are injected via GitHub Actions `secrets` + `env:` so the job environment is the single source of truth.
- The reusable workflow pins the `awdit` ref separately from the analyzed repo's checkout, so a malicious PR cannot swap the audit tool underneath CI.

## Consequences

- **Review gets CI-oriented surface area it did not have before.** `--ci`, `--pr`, machine-readable summary JSON, and a reusable `workflow_call` workflow all land on review, not swarm. Tracked in [roadmap/review-ci-workflow.md](../roadmap/review-ci-workflow.md).
- **Swarm stays small.** No new CI-only flags or modes, no sticky-comment renderer, no GitHub API calls. Simpler to reason about.
- **The archived `swarm-pr-workflow-todo.md` is not discarded.** The still-relevant parts (per-PR file list, `--ci` semantics, `final_summary.json` shape, sticky PR comment) move verbatim to the new review-CI roadmap, rescoped to `awdit review`.

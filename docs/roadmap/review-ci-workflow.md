# Roadmap: `awdit review` CI workflow

- **Status:** Not started
- **Applies to:** `awdit review`
- **Related decisions:** [decisions/0001-review-pipeline.md](../decisions/0001-review-pipeline.md), [decisions/0003-command-split-and-workflows.md](../decisions/0003-command-split-and-workflows.md), [decisions/0004-scope-and-file-selection.md](../decisions/0004-scope-and-file-selection.md)
- **Salvaged from:** [archive/2026-04-17-swarm-pr-workflow-todo.md](../archive/2026-04-17-swarm-pr-workflow-todo.md) (rescoped from `awdit swarm` to `awdit review`)

## Goal

Ship a reusable GitHub Actions workflow that runs `awdit review` against an adopting repository on every same-repo PR, comments a sticky result back on the PR, and uploads the full run directory as a workflow artifact. The workflow must work against arbitrary adopting repos without requiring them to commit awdit config.

## Motivation

Per ADR 0003, `awdit review` is the CI-shaped command: it is the more robust pipeline, it already supports PR-style targeting semantics, and the PR-comment interaction model fits review output more naturally than swarm's launch-and-wait operator model. The earlier swarm-PR workflow plan (see archived doc) had the right mechanics on the wrong command; this roadmap item moves those mechanics onto review.

## Open tasks

### CLI surface on `awdit review`

- Add `--ci` flag that runs non-interactively. `--ci` must:
  - skip all prompt-based flows (danger-map approval, shared-resource review, launch confirmation, config-override menu)
  - auto-accept the existing or newly generated danger map
  - auto-accept the auto-discovered shared resource manifest
  - keep normal interactive behavior unchanged when `--ci` is not passed
- Add `--pr <PR-NUMBER>` as a new review target shape. Semantics:
  - resolves the PR's changed file list (added / modified / renamed / copied), excluding removed files
  - the resolved file list narrows the review target; `scope.exclude` still applies
  - reject if the PR is on a fork and the run is CI-mode, unless explicitly opted in via a safety flag (see trust boundaries in ADR 0003)
- Add `--eligible-file-list PATH` as a lower-level alternative to `--pr`. Semantics:
  - newline-delimited repo-relative paths
  - normalized via existing repo-relative path validation
  - deduped, with paths escaping the repo rejected
  - missing / deleted / symlink files skipped
  - renamed files kept by their current PR-relative path
  - exit successfully with a clear summary when no processable files remain
- `--config PATH` and `--env-file PATH` already exist; confirm both continue to work under `--ci` and load from outside the analyzed repo.

### Machine-readable run summary

- Write `runs/<run_id>/reports/final_summary.json` alongside the existing Markdown reports.
- Include at minimum:
  - `run_id`
  - `target_shape` (e.g., `pr`, `diff`, `files`)
  - `files_requested`, `files_reviewed`
  - per-stage counts: hunter findings, ledger entries, skeptic verdicts, referee verdicts, confirmed bugs, chosen fixes
  - `top_findings` with stable IDs, one-line summaries, severity, and citation paths
  - report file paths for the Markdown artifacts
- Keep all existing Markdown reports unchanged. JSON is additive, not replacing.

### Generic CI config defaults

- Keep `config/config.toml` repo-agnostic by default so CI can point at the bundled awdit config.
- Disable any approval-style or human-in-the-loop behavior when `--ci` is set.
- Make repo-memory writes non-blocking in CI: CI runs may read repo-scoped memory but should not require it to exist, and their memory writes should be best-effort.
- Surface a single `OPENAI_API_KEY` environment path for CI credentials.

### Reusable GitHub Actions workflow

- Expose the workflow through `workflow_call` so adopting repos can invoke it with `uses:`.
- Minimal permissions:
  - `contents: read`
  - `pull-requests: write`
- PR-scoped concurrency with `cancel-in-progress: true`.
- Job steps:
  1. Check out the target repo PR head.
  2. Check out a pinned `awdit` ref into a sibling directory.
  3. Install Python and `uv`.
  4. Collect the PR's changed file list via the GitHub PR files API (or pass `--pr` and let awdit resolve it internally).
  5. Run `uv run --project <awdit-checkout> awdit review --ci --config <awdit-checkout>/config/config.toml --pr <pr-number>`.
  6. Upload the full `runs/<run_id>/` directory as a workflow artifact.
  7. Write a concise job summary (Markdown) pulling from `final_summary.json`.
  8. Create or update one sticky PR comment including processed file count, confirmed-bug count, top findings, and an artifact reference.
- Trust boundaries:
  - Fork PRs are skipped by default (running fork-controlled code with the adopting repo's `OPENAI_API_KEY` is unsafe).
  - Secrets flow through GitHub Actions `secrets` and `env:` only — never `.env` files in CI.
  - The `awdit` checkout is pinned separately so a malicious PR cannot swap the audit tool.

### Adopter-facing documentation

- Document the calling workflow shape for consumer repos, including recommended triggers:
  - `pull_request.opened`
  - `pull_request.synchronize`
  - `pull_request.reopened`
  - `pull_request.ready_for_review`
- Document required secret setup for `OPENAI_API_KEY`.
- Document that consumer repos do not need to commit their own awdit config.
- Document the fork-PR skip default and how to opt in.

### Test plan

- CLI tests proving `--ci` never reads interactive input.
- Tests proving `--config` / `--env-file` load external trusted config and secrets.
- Tests proving `--pr` and `--eligible-file-list` correctly narrow the review target within `scope.exclude`.
- Tests for deduping, path-escape rejection, missing / deleted / symlinked entries.
- Tests for the empty-processable-file exit path.
- Tests for `final_summary.json` shape and counts.
- A helper or snapshot test for PR comment rendering from the JSON output.
- One manual smoke test against a synthetic PR file list before cutting the workflow.

## Dependencies

- ADR 0003 command split is accepted — CI is a `review`-only concern.
- ADR 0004 scope rules are accepted — `scope.exclude` is the only file filter, no `[swarm.files]` profile.
- Implementation follow-up from ADR 0004 (removing `[swarm.files]` parsing) should land first so swarm-side PR plumbing is fully gone before `awdit review` gains the PR-targeting surface. Otherwise the two paths temporarily coexist and confuse operators.

## Status

Not started. This roadmap supersedes the archived `swarm-pr-workflow-todo.md`; that doc's contents should not be implemented as-is.

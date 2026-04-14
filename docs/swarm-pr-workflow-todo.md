# TODO: Reusable PR Swarm Workflow

## Summary

Build a reusable GitHub Actions workflow that runs `awdit swarm` against the exact files changed in each PR, works for arbitrary adopting repos, comments results back to the PR, and uploads full run artifacts.

The implementation should add a CI-safe `awdit swarm` mode, use a trusted bundled CI config instead of repo-controlled config, and expose machine-readable outputs so the workflow does not need to scrape markdown.

## TODO

### 1. Add a CI-safe swarm CLI path

- [ ] Add `awdit swarm --ci` to run non-interactively.
- [ ] Make `--ci` skip all prompt-based flows:
  - danger-map approval/regeneration prompts
  - shared-resource review prompts
  - launch confirmation
  - config override menu
- [ ] Make `--ci` auto-accept the generated or existing danger map.
- [ ] Make `--ci` auto-accept shared resources.
- [ ] Keep normal interactive behavior unchanged when `--ci` is not passed.

### 2. Allow trusted config injection

- [ ] Add `--config PATH` so CI can load config from outside the analyzed repo.
- [ ] Ensure CLI uses the passed config path instead of the target repo's `config/config.toml`.
- [ ] Keep repo-root config as the default when `--config` is omitted.

### 3. Allow explicit PR file targeting

- [ ] Add `--eligible-file-list PATH` with newline-delimited repo-relative file paths.
- [ ] Make this explicit list override profile-based file discovery.
- [ ] Normalize each path with existing repo-relative path validation.
- [ ] Dedupe repeated entries.
- [ ] Reject paths that escape the repo.
- [ ] Skip missing files, removed files, and symlinks.
- [ ] Keep renamed files by their current PR-relative path.
- [ ] Exit successfully with a clear summary when no processable files remain.

### 4. Add machine-readable swarm summary output

- [ ] Write `final_summary.json` alongside the existing markdown reports.
- [ ] Include:
  - `run_id`
  - `eligible_files_requested`
  - `eligible_files_processed`
  - `seed_findings_surfaced`
  - `promoted_issue_candidates`
  - `findings_kept_after_proof`
  - `filtered_after_proof`
  - `top_findings`
  - report file paths
- [ ] Keep existing markdown reports unchanged.

### 5. Bundle a trusted CI config for foreign repos

- [ ] Add a bundled CI config and prompt set inside `awdit`.
- [ ] Configure it to use `OPENAI_API_KEY`.
- [ ] Disable any approval-style or human-in-the-loop startup behavior for CI.
- [ ] Avoid repo-specific shared resource assumptions.
- [ ] Disable repo memory or make it fully non-blocking in CI.
- [ ] Make the reusable workflow always use this trusted config.

### 6. Add a reusable GitHub Actions workflow

- [ ] Create a reusable workflow exposed through `workflow_call`.
- [ ] Set minimal permissions:
  - `contents: read`
  - `pull-requests: write`
- [ ] Add PR-scoped concurrency with cancel-in-progress.
- [ ] Check out the target repo PR head.
- [ ] Check out a pinned `awdit` ref separately.
- [ ] Install Python and `uv`.
- [ ] Collect changed PR files from the GitHub PR files API.
- [ ] Include `added`, `modified`, `renamed`, and `copied` files.
- [ ] Exclude `removed` files.
- [ ] Write the changed file list to the CLI input file.
- [ ] Run `awdit swarm --ci --config ... --eligible-file-list ...`.
- [ ] Upload the full run directory as a workflow artifact.
- [ ] Write a concise job summary.
- [ ] Create or update one sticky PR comment with:
  - processed file count
  - findings kept after proof
  - top findings
  - artifact reference

### 7. Document the adopter workflow

- [ ] Document the calling workflow for consumer repos.
- [ ] Recommend triggering on:
  - `pull_request.opened`
  - `pull_request.synchronize`
  - `pull_request.reopened`
  - `pull_request.ready_for_review`
- [ ] Document required secret setup for `OPENAI_API_KEY`.
- [ ] Document that consumer repos do not need their own awdit config for this workflow.
- [ ] Document that fork PRs are skipped by default for secret safety.

## Test Plan

- [ ] Add CLI tests proving `--ci` never reads interactive input.
- [ ] Add tests proving `--config` loads external trusted config.
- [ ] Add tests proving `--eligible-file-list` overrides profile-based discovery.
- [ ] Add tests for deduping, escaping paths, missing files, deleted files, and symlink exclusion.
- [ ] Add tests for the empty-processable-file case.
- [ ] Add tests for `final_summary.json` shape and counts.
- [ ] Add workflow-side tests or helper tests for PR comment rendering from JSON output.
- [ ] Do one manual smoke test against a synthetic PR file list.

## Assumptions

- Same-repo PRs are supported by default.
- Fork PRs are skipped by default because running untrusted fork code with the adopting repo's `OPENAI_API_KEY` is unsafe.
- Reportable findings are surfaced via PR comment, summary, and artifacts, but do not fail the workflow by default.
- "Every file changed in the PR" means the explicit PR file list is the source of truth, not `eligible_file_profile`.

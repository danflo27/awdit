# 0004. Scope and file selection: `git ls-files` minus `scope.exclude` as the baseline

- **Status:** Accepted — 2026-04-17
- **Applies to:** `awdit review`, `awdit swarm`
- **Related:** [0002-swarm.md](0002-swarm.md), [0003-command-split-and-workflows.md](0003-command-split-and-workflows.md)
- **Follow-up:** [roadmap/review-ci-workflow.md](../roadmap/review-ci-workflow.md), [roadmap/open-questions.md](../roadmap/open-questions.md)

## Context

The previous design had two parallel file-selection surfaces: `scope.{include, exclude}` (shared between review and swarm) and a swarm-specific `[swarm.files].profile` with values `code_config_tests` (default) and `pr_changed_files`. That meant swarm's default was narrower than "the whole repo" despite being marketed as a repo-wide black-hat sweep, and the PR-changed-files mode existed on the wrong command.

The new direction is: one scope surface, shared between both commands, with per-command targeting layered on top.

## Decision

### Single scope primitive

- `scope.include` and `scope.exclude` are the shared scope primitive. Globs, relative to the repo root.
- `scope.include = []` means "no include filter" (everything passes include). It does **not** mean "include nothing."
- `scope.exclude` filters matches out regardless of include state.

### Default file set

The default set of files considered by any awdit run is:

```
{ files returned by `git ls-files` }  minus  { files matching scope.exclude }
```

If `scope.include` is non-empty, the result is further intersected with `scope.include`. The default checked-in config leaves `include` empty.

### Per-command targeting

- **`awdit swarm`** uses the default file set as its eligible-file set. **It does not have its own file profile.** The `[swarm.files]` config block is removed from the decision surface; see "Implementation follow-up" below.
- **`awdit review`** supports all the `feature review` target shapes described in [0001-review-pipeline.md](0001-review-pipeline.md) (diff / commit / file list / PR / pasted text) plus the `full repo review` shape. All target shapes operate *within* the default file set — targeting narrows, it does not escape scope excludes.
- **PR-targeting for `awdit review`** (the `pr_changed_files` concept, formerly on swarm) lives here. The mechanics are tracked in [roadmap/review-ci-workflow.md](../roadmap/review-ci-workflow.md) (per-PR file list fed via an explicit path or computed from the PR API).

### What "everything minus exclude" implies in practice

- Binary assets, lockfiles, generated fixtures, etc. are included by default if they are tracked by git. Operators who want a narrower sweep add exclude globs, not a profile.
- `.venv/`, `__pycache__/`, `.egg-info/`, `.env`, and `.env.*` remain in the default-checked-in exclude list.
- Symlinks and files excluded by `.gitignore` are already absent from `git ls-files` output and need no separate handling.

### Config shape (illustrative)

```toml
[scope]
include = []                     # empty = no include filter
exclude = [
  "**/__pycache__/**",
  "**/*.egg-info/**",
  ".venv/**", "venv/**",
  ".env", ".env.*",
]
```

No `[swarm.files]` block. No `profile` key.

## Rationale

- **One primitive, not two.** The old surface forced operators to reason about scope globs AND a file-profile enum for swarm. The new surface collapses that to one thing.
- **Default matches the "black-hat repo sweep" mental model.** Swarm is marketed as repo-wide; its default should include everything tracked, not just source code by extension.
- **PR targeting belongs on review.** Per 0003, review is the CI-targeted command. Moving the PR-changed-files concept onto review consolidates PR-shaped work under one command.
- **Honest defaults over clever filtering.** If the operator wants to exclude tests, prompts, or fixtures from swarm, exclude globs make that intent explicit in `config.toml`. A hidden profile silently doing the same is worse for debuggability.

## Consequences

- **Slightly larger default swarm runs.** Swarm on a previously-run repo will now see more files per sweep unless the operator adds excludes. Document this prominently in README and release notes when the implementation lands.
- **`config.toml` becomes simpler.** The `[swarm.files]` block goes away; the generic checked-in config shrinks.
- **Review gains PR targeting.** Tracked as a roadmap item, not a decision — see [roadmap/review-ci-workflow.md](../roadmap/review-ci-workflow.md).

## Implementation follow-up (not yet done)

This ADR records the decision; the code has not yet been changed. Current state in `src/`:

- `src/config.py` still parses `[swarm.files].profile` (values: `code_config_tests`, `pr_changed_files`) and defaults it to `code_config_tests`.
- `src/swarm.py::list_eligible_swarm_files` still branches on that profile.
- `src/cli.py::_handle_swarm` still accepts `--base-ref` which is only meaningful for `pr_changed_files`.

Work to land this ADR in code:

1. Remove `[swarm.files]` parsing from `src/config.py`; remove the `eligible_file_profile` field from the swarm config dataclass.
2. Replace `list_eligible_swarm_files` profile branching with a single path that returns `git ls-files` minus `scope.exclude` (respecting `scope.include` when non-empty).
3. Remove the `--base-ref` argument from `src/cli.py::_handle_swarm`.
4. Remove swarm-side `pr_changed_files` plumbing. If still useful, port the per-PR file-list mechanics to `awdit review` as part of [roadmap/review-ci-workflow.md](../roadmap/review-ci-workflow.md).
5. Update `config/config.toml` (both the repo default and `init-config` scaffold) to drop the `[swarm.files]` block.
6. Update tests in `tests/` that cover swarm profile selection.

# 0005. Storage and artifacts: awdit-data-root layout, resource folders, forward-facing Markdown rule

- **Status:** Accepted — 2026-04-17
- **Applies to:** `awdit review`, `awdit swarm`, and all managed on-disk state
- **Related:** [0001-review-pipeline.md](0001-review-pipeline.md), [0002-swarm.md](0002-swarm.md)
- **Supersedes:** the storage sections of [archive/2026-04-17-architecture.md](../archive/2026-04-17-architecture.md) and [archive/2026-04-17-PROPOSED_FILE_STRUCTURE_CONFIG_BEHAVIOUR.md](../archive/2026-04-17-PROPOSED_FILE_STRUCTURE_CONFIG_BEHAVIOUR.md)

## Context

awdit stores two very different kinds of data: immutable per-run artifacts that must be auditable after the fact, and evolving per-repo intelligence that accumulates across runs. Keeping those separate — and keeping the analyzed repo itself clean of awdit-managed state — is load-bearing for both debuggability and for running awdit against arbitrary foreign repos.

This ADR locks the storage model and the artifact contract.

## Decision

### Data root

- `<awdit-data-root>` defaults to the awdit checkout location.
- Operators may override it with `AWDIT_DATA_ROOT`.
- The analyzed repo (current working directory when awdit runs) remains the source of truth for repo identity, git-aware inspection, tracked-file enumeration, config lookup, and scope filtering. **awdit-managed state never lives in the analyzed repo.**

### Top-level layout under the data root

```
<awdit-data-root>/
  config/                         # human-managed defaults (checked into awdit)
    config.toml
    prompts/
    resources/
      shared/                     # auto-discovered shared defaults
      slots/
        hunter_1/ hunter_2/
        skeptic_1/ skeptic_2/
        referee_1/ referee_2/
        solver_1/ solver_2/
  repos/
    <repo_key>/                   # evolving per-repo intelligence
      danger_map.md
      danger_map.json
      memory/
        repo_comments.md
      cases/
        canonical_index.json
  runs/
    <run_id>/                     # immutable per-run artifacts
      run.json
      prompts/                    # prompt snapshots used for the run
      derived_context/            # role digests, swarm digest, etc.
      resources/
        shared/
          manifest.md
          staged/
        slots/<slot>/
          manifest.md
          staged/
      issues/                     # run-local case files
      reports/
        debate/
      validation/
        baseline/
      solvers/
      swarm/                      # (swarm runs only; see below)
      logs/
      session_state/              # slot/session control-plane artifacts
  worktrees/
    <run_id>/
      solver_1/
      solver_2/
      selected/
  state/
    awdit.db                      # persistent app state (sqlite)
    scoreboard/
```

### Review vs. swarm run subtrees

Review runs produce artifacts under the top-level `runs/<run_id>/` directories above (`issues/`, `reports/`, `validation/`, `solvers/`). Swarm runs additionally produce:

```
runs/<run_id>/swarm/
  seeds/                           # raw per-worker markdown + JSON
  proofs/                          # proof notes, exploit steps, citations, repro artifacts
  reports/
    seed_ledger.md                 # all initial seeds and zero-finding outcomes
    case_groups.md                 # duplicate and related-seed grouping under SWM-###
    final_ranked_findings.md       # primary operator-facing ranked report
    final_summary.md               # short run summary with top-finding links
```

### Repo-scoped vs. run-scoped split

- **Repo-scoped (`repos/<repo_key>/`)** holds living intelligence: the danger map, truth-labeled memory, repo comments, canonical case index linking across runs.
- **Run-scoped (`runs/<run_id>/`)** holds immutable artifacts: the exact prompts, derived digests, resource snapshots, agent reports, debate transcripts, validation output, and final reports for one run.
- Run-local issue files are immutable historical snapshots. Repo-scoped memory evolves over time. Canonical linking connects related issues across runs **without mutating old run-local issue files.**
- Repo-scoped memory does not store external-resource selections. Those belong to the run.

### Resource folder discovery

- `config/resources/shared/` is auto-included for every run.
- `config/resources/slots/<slot>/` is auto-included for that slot.
- Operators almost never need `include` — the folders *are* the include. The common case is `[resources.shared] exclude = []` and per-slot `exclude = []` overrides when a particular slot needs less.
- `include` is reserved as an escape hatch for explicit URLs or out-of-tree paths.
- Before launch, the CLI shows the effective resource list with a `Y / e / n` prompt: accept as shown, replace the exact list for this run only, or exit the wizard.
- The CLI states explicitly that those folders are included by default unless excluded.
- The final chosen resources are persisted only under the run-scoped area.
- v1 implementation stages local files and folders into `runs/<run_id>/resources/{shared,slots/<slot>}/staged/`. URLs are recorded in the manifests without being fetched.

### Forward-facing Markdown artifact rule

- Every code-oriented stage must produce a human-reviewable Markdown artifact, not just raw JSON or logs.
- The terminal surfaces clickable Markdown paths at each major stage.
- Expected Markdown artifacts (non-exhaustive):
  - repo danger map
  - shared resource manifest for the run
  - per-slot resource manifests when attachments exist
  - candidate ledger summary
  - run-local issue files
  - skeptic summaries or raw skeptic reports
  - merged referee report
  - validation summary
  - referee fix-review reports
  - merged solver comparison summary
  - final solver selection summary
  - (swarm) seed ledger, case groups, final ranked findings, final summary
- These Markdown artifacts should reference the relevant code paths and line spans wherever code is central to the stage.

### Session-state (control plane)

`runs/<run_id>/session_state/` is the run-local home for slot/session control-plane artifacts. It should be able to explain, after the fact, which visible slot identity was on which session epoch and why an epoch changed. Expected artifact families:

- Session epoch records per slot identity.
- Dispatch records, including fresh packet-revision dispatches.
- Heartbeat and other lifecycle metadata owned by the awdit worker layer.
- Checkpoint references for completed dispatches.
- Compaction and rehydration artifacts, including recovery records after attached-provider failure.

Heartbeat and lifecycle data are append-only events plus occasional immutable snapshots rather than in-place mutable state. Exact filenames, table schemas, and indexing details remain open — see [roadmap/open-questions.md](../roadmap/open-questions.md).

### Artifact invariants

- Run-local artifacts are immutable. Repo-scoped memory evolves.
- Canonical linking across runs never mutates old run-local issue files.
- Prompt snapshots exist for auditability; runtime execution reads the prompt files declared in `config/config.toml`.
- Provider handles may be referenced from `session_state/`, but they are attached warm state only, not the canonical slot identity.

## Consequences

- **Cross-repo execution is first-class.** Operators can `uv run --project /path/to/awdit awdit ...` from any target repo; awdit-managed state never contaminates the target working tree.
- **Auditability is structural.** Anyone can re-read a run directory months later and reconstruct what prompts, resources, and models produced the final report.
- **Swarm and review share one home.** Both commands write under `runs/<run_id>/`; swarm adds one additional subtree (`swarm/`) rather than living in its own parallel directory.
- **Resource discovery is obvious.** Dropping a file into `config/resources/shared/` is the entire onboarding step for a new default resource.

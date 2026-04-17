# Roadmap: Open questions

- **Status:** Tracking doc (will never fully "complete" — items graduate into ADRs or get implemented and drop off)
- **Related decisions:** [decisions/0001-review-pipeline.md](../decisions/0001-review-pipeline.md), [decisions/0002-swarm.md](../decisions/0002-swarm.md), [decisions/0005-storage-and-artifacts.md](../decisions/0005-storage-and-artifacts.md)
- **Salvaged from:** the `TBD` and `Open Areas` sections of [archive/2026-04-17-architecture.md](../archive/2026-04-17-architecture.md)

## Goal

Keep a single place to track design questions that are deliberately left open — so they are not forgotten and not silently decided by whoever edits a file next. Each entry should be specific enough that someone can pick it up, answer it, and either write a follow-up ADR or update an existing one.

## Motivation

The archived architecture draft carried a long tail of `TBD` markers. Rather than copying those markers into the new ADRs (which should only record decisions that have been made), the open items live here as an explicit to-decide list. That way ADRs stay concise and opinionated, while the unresolved questions remain visible and assignable.

## Open tasks

Each item below is a question awaiting a decision or implementation. When one is answered, record the answer in the appropriate ADR (or write a new one) and remove the entry from this list.

### Coordinator mechanics

- Exact policy for converting hunter-stage candidate clusters into canonical issue packets (cluster → packet identity rules, tie-breaks, lineage preservation).
- Exact coordinator policy for replacing an already-pending dispatch during same-packet supersession. Who decides, what operator approval (if any) is required, and what is logged.
- Exact way referees consume unresolved skeptic disagreement without redundant re-investigation (inputs, summarization, artifact handoff).
- Exact wording and ownership of any packet-level summaries that may still be useful downstream — including whether the coordinator authors them at all (ADR 0001 leans "no").
- Exact `dispatch envelope` schema (fields, required vs optional, acknowledgment format).
- Exact `slot checkpoint` schema and the evidence-addressing contract it must satisfy for rehydration.
- Exact `issue packet revision` schema and how revisions chain.
- Exact `coordinator action log` schema and retention policy.

### Session / slot lifecycle

- Exact provider-specific cache behavior and performance tradeoffs for attached warm handles (OpenAI Responses API vs any future providers).
- Exact budgeting policy for context-threshold-driven compaction — is there a configurable override, or are the 75% / 90% thresholds hard-coded?

### Persistence

- Exact SQLite schema for `state/awdit.db` (scoreboard, any canonical indices that outlive a single run).
- Exact artifact naming conventions under `runs/<run_id>/` beyond the families listed in ADR 0005 (e.g., per-stage filename patterns, timestamping rules).
- Exact worktree cleanup policy for `worktrees/<run_id>/` (when a run is discarded vs integrated vs left as historical).
- Exact repo-memory summarization and pruning policy: when does `repos/<repo_key>/memory/` get compacted, by what process, and with what operator confirmation.
- Exact filenames and indexing for `runs/<run_id>/session_state/` artifact families (epoch records, dispatch records, heartbeats, checkpoints, rehydration).

### Scoring / validation

- Exact score formulas for whatever scoreboard or ranking surface the `scoreboard` command exposes.
- Exact validation / disqualification policy for solver outputs before they reach referee fix comparison (beyond whatever `validation.checks` covers).

### Provider interface

- Exact provider interface shape (abstract methods, error taxonomy, retry semantics) if/when a second provider lands.
- Exact provider-handle recovery path when an attached warm provider handle fails mid-dispatch vs between dispatches (partly addressed in ADR 0001 via epoch rotation, but the concrete artifact-writing sequence is still unspecified).

### Prompts / resources

- Exact wording of slot prompts is intentionally open — `config/prompts/*.md` are edited in-tree rather than fixed here.
- Exact per-resource persisted metadata under `runs/<run_id>/resources/` (manifest fields beyond path/URL/timestamp).
- Exact staging mechanism for URL resources — currently recorded-not-fetched in v1; a later decision may introduce snapshot fetching with cache semantics.

## Dependencies

- ADRs 0001 through 0005 are the authoritative decisions today. New answers should be written as follow-up ADRs (numbered 0006+) rather than edited into the existing ones, so the decision history remains append-only.

## Status

Live document. Edit as items are resolved. When this list shrinks meaningfully, reorganize by topic rather than letting entries pile up chronologically.

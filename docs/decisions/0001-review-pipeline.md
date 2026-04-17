# 0001. Review pipeline: competing agents, bounded debate, human review, warm slot sessions

- **Status:** Accepted — 2026-04-17
- **Applies to:** `awdit review`
- **Related:** [0002-swarm.md](0002-swarm.md), [0003-command-split-and-workflows.md](0003-command-split-and-workflows.md), [0005-storage-and-artifacts.md](0005-storage-and-artifacts.md)
- **Supersedes:** [archive/2026-04-17-architecture.md](../archive/2026-04-17-architecture.md), [archive/2026-04-17-agent-isolation-workflow.md](../archive/2026-04-17-agent-isolation-workflow.md)

## Context

`awdit review` is the primary security-audit command. It exists to produce a high-confidence audit report by running two competing model pipelines against the same target, letting them disagree in bounded ways, and ending with a human-in-the-loop truth review before any fix work begins.

This ADR records the pipeline shape and the runtime rules that keep the pipeline auditable. It does not record storage paths (see 0005), file selection (see 0004), or the `awdit review` vs. `awdit swarm` command split (see 0003).

## Decision

### Pipeline stages (fixed order)

1. Danger map — repo-scoped living intelligence, generated or refreshed with operator approval before review begins.
2. Review target — either a feature-review target (diff / commit / file list / PR / pasted text) or the whole repo.
3. Scope rules + model lineup + derived role digests.
4. Hunters — two independent slots compete on the same target. Recall-first; may overgenerate. Every finding cites exact file paths and line references. No hunter-to-hunter chat.
5. Shared candidate ledger — coordinator-owned, cluster-first normalization of hunter output into stable finding IDs.
6. Skeptics — two slots challenge or accept existing findings. Skeptics cannot introduce new findings.
7. Bounded skeptic-to-skeptic debate (at most two turns per side) only when the coordinator detects disagreement on one issue packet revision.
8. Issue packets — coordinator assembles hunter + skeptic artifacts into packets with stable IDs, revisions, citations, and provenance.
9. Referees — two tool-using slots render `REAL BUG / NOT A BUG` verdicts on the full packet set.
10. Bounded referee-to-referee rebuttal (at most two turns per side) only when the coordinator detects disagreement on one packet revision.
11. Merged referee report — coordinator performs a citation-preserving merge; unresolved disagreements are forwarded cleanly rather than flattened.
12. Human truth review — per-issue `yes / no / unsure`. Only `yes` issues advance. Issues with an undisputed `NOT A BUG` outcome after both skeptic and referee challenges are auto-dismissed (still recorded).
13. Solvers — two slots work from the confirmed bug list, each in its own git worktree, one commit per confirmed bug.
14. Shared baseline fix validation — configured `validation.checks` plus any exploit/trigger replay, static re-analysis, and nearby-variant scan.
15. Referee fix comparison — referees re-enter to compare solver outputs against the shared baseline facts.
16. Human fix selection — per-bug `Solver 1 / Solver 2`. Integration branch applies chosen commits in finding-ID order.

### Role rules

- There are eight fixed slots: `hunter_1`, `hunter_2`, `skeptic_1`, `skeptic_2`, `referee_1`, `referee_2`, `solver_1`, `solver_2`.
- Referees are the only source of `REAL BUG / NOT A BUG` truth. There is no separate pre-solver validator.
- Hunters never chat with each other; solvers never debate each other; cross-family chat is disallowed. Direct bounded debate is permitted only skeptic-to-skeptic and referee-to-referee.
- `referee_1` and `referee_2` share the same canonical base behavior by default, while remaining easy to override per-prompt.

### Coordinator role (traceable assembler, not judge)

- The coordinator normalizes, validates, routes, persists, revises packet identifiers, and compiles slot-authored material with citations.
- It performs mechanical merges and stage transitions that can be traced back to cited slot artifacts.
- It must not author new substantive code-truth, exploit-quality, or fix-quality judgments on its own.
- It owns cluster-first ledger assembly, debate threading, merged-report assembly, and packet revisioning.

### Agent isolation & slot/session rules

- **Visible slot identity.** One per configured slot per run (e.g., `Hunter 1`). Public-facing label. Custom names may be added later; slot labels are the default.
- **Live slot session.** An awdit-owned runtime with one lease, one current session epoch, and at most one attached provider handle. Warm-first in v1.
- **Session epoch.** One concrete incarnation record. Created at run start as a reserved marker only; not live until the first dispatch. Compaction or attached-provider failure creates a new epoch for the same visible slot identity.
- **Dispatch.** One immutable work assignment with explicit inputs and expected outputs.
- **Provider handle.** Disposable warm provider-side state attached to the current epoch. Not the slot identity.
- **Lazy start.** No slot workers start eagerly at run launch. A slot starts only when the coordinator dispatches work to it for the first time.
- **One active + one pending dispatch** per slot. Unrelated work cannot displace a pending dispatch. (Same-packet supersession rules remain open — see [roadmap/open-questions.md](../roadmap/open-questions.md).)
- **Worker heartbeats** are the canonical liveness signal. Provider activity is secondary metadata only.
- **Context-usage thresholds.** 75% triggers an advisory CLI warning. 90% forces compaction at the next safe boundary. Mid-dispatch compaction is reserved for hard-failure recovery, not the normal path.
- **Rehydration must be checkpoint-driven.** A completed dispatch writes a slot-authored checkpoint. Compaction and failure recovery write checkpoint or recovery artifacts before the next epoch starts. Rehydration is grounded in evidence-addressed, slot-authored checkpoints + referenced prior artifacts, not hidden coordinator paraphrase.
- **Fixed-for-the-run.** Prompts, models, review targets, and operator-selected startup resources are fixed once a run launches. Changing them ends the current run and starts a new one.

### Bounded debate rules

- Debate opens only when the coordinator detects disagreement on one issue packet revision.
- Each debate thread is scoped to that one revision, with separately prepared strongest bundles, capped at two turns per side.
- Each side may read the opposing artifact and the current live rebuttal history for that thread.
- If substantive new evidence appears after debate opens, the coordinator creates a new packet revision and dispatches it explicitly rather than mutating the live debate.
- Debate transcripts are append-only artifacts.
- Unresolved disagreements after the allowed turns are forwarded cleanly and minimally in the merged report.

### Review shapes (targeting)

- `Feature review`: diff-against-main, single commit, explicit file list, GitHub PR, or pasted diff/text. Local git refs are the default when both local and GitHub inputs would work.
- `Full repo review`: the whole repo under scope rules.

See [0004-scope-and-file-selection.md](0004-scope-and-file-selection.md) for how scope rules interact with targeting.

### CLI / UX contract

- The primary UX is an interactive CLI wizard.
- A new run ID is created per attempt. v1 does not support resuming interrupted runs.
- The `scoreboard` subcommand is a separate command backed by local persistent state. (Not yet implemented.)
- Every code-oriented stage produces a forward-facing Markdown artifact. The terminal surfaces clickable Markdown paths. See [0005-storage-and-artifacts.md](0005-storage-and-artifacts.md) for artifact families and the Markdown-artifact rule.

## Consequences

- **Higher robustness at the cost of cycles.** Two independent passes plus bounded debate plus human review produce more trustworthy verdicts but cost roughly 2x the baseline token spend.
- **Coordinator complexity.** The coordinator owns a non-trivial amount of mechanical logic (ledger, debate threading, merging, revisioning, checkpoint validation). Keeping it procedural rather than substantive is the hard design constraint.
- **Runs are immutable; repo memory evolves.** This split is load-bearing for auditability and is recorded in [0005-storage-and-artifacts.md](0005-storage-and-artifacts.md).
- **Automation compatibility.** The pipeline is amenable to CI execution — see [0003-command-split-and-workflows.md](0003-command-split-and-workflows.md) for the `awdit review` CI story and [roadmap/review-ci-workflow.md](../roadmap/review-ci-workflow.md) for the pending workflow work.

## Invariants (quick-reference)

- Stage order is fixed (see pipeline stages above).
- Referees decide bug truth before solver handoff.
- Referees compare fixes only after shared baseline validation has run.
- One visible slot identity per run; at most one live session per slot at a time.
- One active + one pending dispatch per slot.
- Compaction and attached-provider failure are the only forced fresh-session events inside a run.
- New evidence causes packet revision, not silent mutation of an open debate context.
- Debate is bounded to skeptic↔skeptic and referee↔referee only.
- Run-local artifacts are immutable; repo-scoped memory evolves.

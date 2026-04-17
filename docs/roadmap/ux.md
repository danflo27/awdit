# Roadmap: CLI / UX polish

- **Status:** Backlog — none of these are blocking
- **Applies to:** `awdit review`, `awdit swarm`, and the shared wizard chrome
- **Related decisions:** [decisions/0001-review-pipeline.md](../decisions/0001-review-pipeline.md), [decisions/0005-storage-and-artifacts.md](../decisions/0005-storage-and-artifacts.md)
- **Salvaged from:** [archive/2026-04-17-prototype-notes.md](../archive/2026-04-17-prototype-notes.md)

## Goal

Capture the UX-polish items that surfaced during prototype testing but don't rise to the level of a design decision. These are quality-of-life improvements to the interactive CLI wizard, mostly around clarity, consistency, and reducing manual ceremony.

## Motivation

Prototype notes accumulated a bunch of "this prompt is confusing" and "this should auto-fill" observations. Rather than leave those buried in a dated notes file, they live here as an actionable backlog. None of them are required for either `awdit review` or `awdit swarm` to be usable, but each of them makes the tool feel less clumsy.

The standing aesthetic rule is **CRAP — contrast, repetition, alignment, proximity**. Terminal sections should look consistent across stages: same heading style, same note-block style, same list formatting, predictable spacing. Ambitious ASCII / TUI styling is explicitly future work; clean structure comes first.

## Open tasks

### Config summary rendering

- Clean up alignment, spacing, and color on the effective-config summary screen. Apply CRAP principles consistently.
- Separate the resource list from the "note for user:" block with clear visual breaks (blank line above and below). Shared resources and per-slot resources should use the exact same layout.
- When no resources are attached for a category, print an explicit "none" rather than leaving the block empty.

### Launch-time prompt wording

- Change the `Y / e / n` prompt for shared resources from "Use / edit / exit" to "proceed / edit / exit". Add blank lines above and below the note for scan-ability.
- Clarify or remove the `Dispatch mode override [Enter=foreground/foreground/background]:` prompt. Either rename the modes to something self-explanatory or drop the prompt and expose the override only via config.

### Auto-fill and remove friction

- Work label and work key should auto-generate with an approve-or-edit confirmation, not free-text prompts on a blank line.
- Remove the "Instructions source [inline/file]: inline" choice entirely. Inline instructions in the CLI are not the right shape — the slot-specific prompt should always come from `config/prompts/<slot>.md`, and the orchestrator should have its own `config/prompts/orchestrator.md` that we generate together later.

### Orchestrator spawn

- Spawn the orchestrator at run start so it can dispatch `hunter_1` with the correct prompt and signal the operator when the hunter finishes. This is the minimum plumbing needed to stop having the operator drive role handoff manually.

### Logging

- Make sure prototype-style runs actually write logs to `runs/<run_id>/logs/`. The prototype silently dropped logs on the floor; that needs to stop.

### Future / nice-to-have

- ASCII-art startup banner and consistent themed chrome throughout. Low priority — functional layout wins over decoration.

## Dependencies

- The `config/prompts/` layout and orchestrator prompt are partially gated on ADR 0001's agent-isolation rules; prompt wording is explicitly `TBD` in [open-questions.md](open-questions.md).
- None of these items block the CI roadmap in [review-ci-workflow.md](review-ci-workflow.md) — CI runs under `--ci` skip the interactive wizard entirely.

## Status

Backlog. Pick up ad hoc as prototype friction hits. When a section ships, remove it from this list.

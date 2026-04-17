# Archive

This directory holds older planning documents that have been superseded by the ADRs under [../decisions/](../decisions/) and the roadmap items under [../roadmap/](../roadmap/). Every file here is frozen as of the date in its filename and begins with an explicit "Archived" pointer at the top explaining where its live content went.

**Do not treat anything in this directory as current.** Read these only for historical context — when you want to understand how the design evolved, why a decision was made, or what alternatives were considered and dropped.

## What's here

### Top-level planning docs (2026-04-17 snapshot)

| File | Superseded by |
|---|---|
| [2026-04-17-architecture.md](2026-04-17-architecture.md) | [decisions/0001-review-pipeline.md](../decisions/0001-review-pipeline.md) + [decisions/0005-storage-and-artifacts.md](../decisions/0005-storage-and-artifacts.md); open items in [roadmap/open-questions.md](../roadmap/open-questions.md) |
| [2026-04-17-agent-isolation-workflow.md](2026-04-17-agent-isolation-workflow.md) | [decisions/0001-review-pipeline.md](../decisions/0001-review-pipeline.md) (agent isolation section) |
| [2026-04-17-repo-wide-black-hat-auditing.md](2026-04-17-repo-wide-black-hat-auditing.md) | [decisions/0002-swarm.md](../decisions/0002-swarm.md); CI bits in [roadmap/review-ci-workflow.md](../roadmap/review-ci-workflow.md) |
| [2026-04-17-swarm-pr-workflow-todo.md](2026-04-17-swarm-pr-workflow-todo.md) | [roadmap/review-ci-workflow.md](../roadmap/review-ci-workflow.md) — mechanics rescoped from `awdit swarm` to `awdit review` |
| [2026-04-17-swarm-task-list.md](2026-04-17-swarm-task-list.md) | Stale status log; no live successor |
| [2026-04-17-prototype-notes.md](2026-04-17-prototype-notes.md) | [roadmap/ux.md](../roadmap/ux.md) |
| [2026-04-17-PROPOSED_FILE_STRUCTURE_CONFIG_BEHAVIOUR.md](2026-04-17-PROPOSED_FILE_STRUCTURE_CONFIG_BEHAVIOUR.md) | [decisions/0005-storage-and-artifacts.md](../decisions/0005-storage-and-artifacts.md) |
| [2026-04-17-e2e-cli-walkthrough.md](2026-04-17-e2e-cli-walkthrough.md) | Aspirational transcript; no live successor. Use the ADRs for the real contract. |
| [2026-04-17-development.md](2026-04-17-development.md) | Essential dev-quickstart content folded into the repo-root [README.md](../../README.md) |

### Research

[research/](research/) contains earlier landscape / challenge / synthesis notes from March 2026. Kept for historical context. Nothing in there supersedes the current ADRs.

## Conventions

- Archived filenames are prefixed with the date the file was archived (YYYY-MM-DD).
- Every archived file's first line is a blockquote explaining where its live content went.
- Archiving is an append-only act: we never edit archived content except to add or correct the "Archived" header. Substantive updates go into ADRs or roadmap docs instead.

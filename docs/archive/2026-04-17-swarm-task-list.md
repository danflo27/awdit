> **Archived 2026-04-17.** No direct successor. This was an ephemeral checkpoint log, not a document. Current open work lives under [roadmap/](../roadmap/). Preserved for historical context; do not treat as current.

# Swarm MVP Task List

## Completed
- Planned the swarm MVP implementation and checkpoint workflow.
- Checkpoint 1: shared foundations
  - added the running swarm task list
  - added swarm prompt and config support
  - added repo identity helpers
  - added minimal SQLite run-state helpers
  - added focused config/repo-state tests
- Checkpoint 2: danger-map flow
  - added the `awdit swarm` command shell
  - added repo danger-map generation and loading helpers
  - added accept, edit-and-regenerate, and regenerate loop support
  - added CLI coverage for the new danger-map startup flow
- Checkpoint 3: swarm startup flow
  - added shared-resource staging for swarm runs
  - added swarm prompt snapshots and run-scoped startup artifacts
  - added swarm digest generation and preflight output
  - added CLI coverage for preflight artifacts and status handling

## In Progress
- Checkpoint 4: sweep execution

## Next
- Checkpoint 5: artifacts, reports, and polish

## Open Risks / Follow-Ups
- The existing config override menu still needs a later UX cleanup pass.
- Danger-map generation will be a single model pass in v1 and should be improved later.
- Proof stage and duplicate grouping are intentionally out of scope for this slice.

## Latest Checkpoint Commit
- `6751650` checkpoint 3: add swarm startup preflight

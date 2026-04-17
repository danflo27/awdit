# awdit

`awdit` is an AI-assisted security audit CLI. It runs two top-level commands against a target repository:

- **`awdit review`** — the primary audit pipeline. Two competing hunter slots, two skeptics, two referees, and two solvers run in a fixed-order pipeline with bounded debate and a human truth review before any fix work. Designed for robustness. Runs locally or in CI.
- **`awdit swarm`** — a broader, cheaper repo-wide offensive sweep. One adversarial worker per eligible file, two-stage sweep → proof, one ranked report. Local only.

The project is early and architecture-led. The live design is captured as ADRs under [docs/decisions/](docs/decisions/); in-flight work is tracked under [docs/roadmap/](docs/roadmap/).

## Design

Start with the ADRs — read them in order:

- [0001 — Review pipeline](docs/decisions/0001-review-pipeline.md) — stages, role rules, coordinator responsibilities, slot/session lifecycle, bounded debate
- [0002 — Swarm](docs/decisions/0002-swarm.md) — one-agent-per-file sweep, proof ladder, configuration surface
- [0003 — Command split and workflows](docs/decisions/0003-command-split-and-workflows.md) — review runs locally or in CI; swarm is local-only
- [0004 — Scope and file selection](docs/decisions/0004-scope-and-file-selection.md) — `git ls-files` minus `scope.exclude` as the shared baseline
- [0005 — Storage and artifacts](docs/decisions/0005-storage-and-artifacts.md) — data-root layout, run-scoped vs repo-scoped split, forward-facing Markdown rule

## Roadmap

- [review-ci-workflow.md](docs/roadmap/review-ci-workflow.md) — `awdit review` in GitHub Actions: `--ci`, `--pr`, machine-readable summary, reusable workflow
- [open-questions.md](docs/roadmap/open-questions.md) — design questions deliberately left open
- [ux.md](docs/roadmap/ux.md) — CLI polish backlog

## Running

Use `uv` for everything.

```bash
cd /path/to/awdit
uv sync                         # create or refresh the env
uv run pytest -q                # run tests
uv run awdit --help             # show commands
uv run awdit list-models        # list live models for the active provider
uv run awdit review             # start a review wizard in the current repo
```

### Running awdit against another repository

awdit-managed state (run artifacts, repo memory, worktrees, local DB) lives under the awdit project root by default — **never** inside the analyzed repo. Cross-repo runs are first class:

```bash
cd /path/to/target-repo
uv run --project /path/to/awdit awdit review \
  --config /path/to/awdit/config/config.toml \
  --env-file /path/to/awdit/.env
```

`--config` and `--env-file` keep config and secrets outside the analyzed repo. Set `AWDIT_DATA_ROOT` if you want managed storage somewhere other than the awdit checkout. See [0005 — Storage and artifacts](docs/decisions/0005-storage-and-artifacts.md) for the full layout.

### Provider credentials

For local runs, awdit reads `OPENAI_API_KEY` from either the shell environment or a repo-root `.env` (shell wins). In CI, never use `.env` — inject the key through GitHub Actions `secrets` and `env:`.

## Repository layout

- `src/` — implementation
- `config/` — checked-in defaults: `config.toml`, `prompts/`, `resources/shared/`, `resources/slots/<slot>/`
- `tests/` — pytest suite
- `docs/decisions/` — ADRs (the live design contract)
- `docs/roadmap/` — in-flight and backlog work
- `docs/archive/` — superseded planning docs, frozen for historical context only ([archive index](docs/archive/README.md))

## Dependency cooldown

Dependency resolution is intentionally conservative. [pyproject.toml](pyproject.toml) sets `[tool.uv] exclude-newer` to a timestamp that represents a rolling ~14-day buffer. Refresh that timestamp deliberately if you want to preserve the same cooldown policy when pulling new dependencies.

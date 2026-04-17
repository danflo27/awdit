> **Archived 2026-04-17.** Essential dev-quickstart content folded into the repo-root [README.md](../../README.md). Preserved for historical context; do not treat as current.

# Development

## Package Manager

Use `uv` for local development and execution.

The project pins a conservative resolver policy in [pyproject.toml](/Users/df/projects/awdit/pyproject.toml):

```toml
[tool.uv]
exclude-newer = "2026-03-15T00:00:00Z"
```

That timestamp represents a 14-day buffer as of 2026-03-29. Update it deliberately and
periodically if you want to preserve the same rolling dependency-cooldown policy.

## First-Time Setup

From the project root:

```bash
cd /Users/df/projects/awdit
uv sync
```

This creates or refreshes the local virtual environment using the project dependencies.

## Running Commands

From the project root:

```bash
uv run pytest -q
uv run awdit --help
uv run awdit list-models
uv run awdit review
```

## Provider Credentials

For local development, `awdit` reads provider credentials from either:

- shell environment variables such as `OPENAI_API_KEY`
- repo-root `.env`

When both are present, shell environment variables win over `.env`.

## Terminal UX

CLI presentation should follow CRAP principles: color, repetition, alignment, and proximity.
Even before a richer TUI exists, repeated terminal sections should use consistent headings,
list formatting, note blocks, and line wrapping so the operator can scan output quickly.

More ambitious ASCII-art or TUI styling is future work. For now, favor clean structure and
predictable layout over decorative output.

## Using `awdit` From Another Repo

If you want to inspect a different repository without installing a global binary, point `uv run`
at this project explicitly:

```bash
cd /path/to/target-repo
uv run --project /Users/df/projects/awdit awdit review
```

That runs the `awdit` project environment while keeping the target repository as the current
working tree for the review.

By default, durable awdit-managed artifacts now accumulate under the awdit project root rather
than under the target repo:

- `repos/<repo_key>/`
- `runs/<run_id>/`
- `worktrees/<run_id>/`
- `state/awdit.db`

If you want those folders somewhere else, set `AWDIT_DATA_ROOT` before launching `awdit`.
The target repo still controls repo identity, tracked-file scope, config lookup, and git-aware
inspection. Only managed storage moves to the shared awdit data root.

If the provider key lives outside the target repository, pass it explicitly for `swarm` runs:

```bash
cd /path/to/target-repo
uv run --project /Users/df/projects/awdit awdit swarm \
  --config /Users/df/projects/awdit/config/config.toml \
  --env-file /Users/df/projects/awdit/.env
```

Use `--config` to choose the config source and `--env-file` to choose the secrets file. Keep
them explicit so cross-repo runs do not depend on the target repo's `.env`.

In GitHub Actions, do not rely on `.env` files. Inject `OPENAI_API_KEY` through workflow
`secrets` and `env:` so the job environment remains the source of truth.

## Undoing An Older Editable `pip` Install

If you previously ran:

```bash
cd /Users/df/projects/awdit
python -m pip install -e .
```

remove it with:

```bash
python -m pip uninstall awdit
```

Then use `uv sync` and `uv run ...` going forward.

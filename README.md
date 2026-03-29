# awdit

`awdit` is a docs-first project for an AI-assisted security audit workflow that coordinates competing hunter, skeptic, referee, and solver agents around a single interactive CLI. The current design keeps visible persistent slot identities per run, cluster-first candidate handling, orchestrator-owned warm slot sessions with checkpoint-based rehydration and disposable attached provider handles, bounded skeptic/referee debate only, and a coordinator that acts as a traceable assembler rather than a hidden substantive judge. The repository is intentionally architecture-first at this stage, with the current design captured in [docs/architecture.md](docs/architecture.md), the canonical slot/session workflow diagram in [docs/agent-isolation-workflow.md](docs/agent-isolation-workflow.md), and a full pretend end-to-end operator transcript in [docs/e2e-cli-walkthrough.txt](docs/e2e-cli-walkthrough.txt).

The current implemented slice is the startup resource flow for `awdit review`:
- awdit loads the effective config and resource defaults
- everything under `config/resources/shared/` and `config/resources/slots/<slot>/` is included by default unless excluded in repo config
- the operator can accept, replace, or exit with the `y / e / n` review flow
- the final selected resources are frozen under `.awdit/runs/<run_id>/resources/`
- local files and folders are staged into the run folder, while URLs are currently recorded in manifests without being fetched

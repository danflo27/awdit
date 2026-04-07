# Repo-Wide Black-Hat Auditing

## Standard Mode

Standard mode remains the main product path that `awdit` is already oriented around:
- a calm visible UX
- a bounded set of robust internal roles
- traceable artifacts
- human review before repair

That mode is still the default identity of the product.

## Swarm Mode

`awdit swarm` is a separate top-level command for a slower repo-wide offensive pass.

It is intentionally not the same thing as normal `awdit review`.
- `awdit review` keeps the current visible-slot architecture and staged case-file pipeline
- `awdit swarm` is a simpler read-only batch mode for broad black-hat hunting across the repo

The user experience should stay quiet and minimal:
- launch the run
- wait
- inspect one organized ranked report with links to raw artifacts

Swarm mode is a deliberate exception to the normal visible-slot pipeline. It exists for deep background-style repo-wide offensive auditing, not as the primary face of the product.

## Official Transcript

Pretend repo: `~/src/tiny-notes-api`

Pretend repo shape:
- `src/app.py`
- `src/auth.py`
- `src/routes/notes.py`
- `src/db.py`
- `tests/test_notes.py`
- `docker-compose.yml`

```text
$ cd ~/src/tiny-notes-api
$ awdit swarm

awdit> Starting new swarm run...
[* create run_id: 2026-04-05_174212 *]
[* create runs/2026-04-05_174212/ *]
[* open sqlite db: state/awdit.db *]
[* insert runs row: mode=swarm status=starting *]

awdit> Repository detected: `tiny-notes-api`
awdit> No repo danger map exists for this repository yet.
awdit> Swarm mode requires a repo danger map before launch.
awdit> Generating repo danger map...
[* create repo key: tiny-notes-api_b81c2f7d *]
[* create repos/tiny-notes-api_b81c2f7d/ *]
[* inspect tracked files, configs, tests, and git metadata *]
[* derive compact trust-boundary summary, risky sinks, auth assumptions, and hot paths *]
[* write repos/tiny-notes-api_b81c2f7d/danger_map.md *]
[* write repos/tiny-notes-api_b81c2f7d/danger_map.json *]
[* write repos/tiny-notes-api_b81c2f7d/memory/repo_comments.md *]

awdit> Repo danger map ready:
awdit>   `repos/tiny-notes-api_b81c2f7d/danger_map.md`
awdit> Review the map, then choose:
awdit>   y. Accept it and continue
awdit>   e. Enter corrections or guidance, then regenerate it
awdit>   n. Regenerate it without extra guidance
awdit> Accept / edit / regenerate? [Y/e/n]
user> y

awdit> Shared resources available for this run:
awdit>   Everything under `config/resources/shared/` is included by default.
awdit>   Repo config usually only needs `[resources.shared] exclude = [...]`.
awdit>   Use `[resources.shared] include = [...]` only for explicit URLs or out-of-tree paths.
awdit>   1. `config/resources/shared/http-threat-notes.md`
awdit> Use / edit / exit? [Y/e/n]
user> y
[* write runs/2026-04-05_174212/resources/shared/manifest.md *]
[* stage local shared resources into the run folder *]

[* render swarm prompt snapshot from config/config.toml *]
[* write prompt snapshot to runs/2026-04-05_174212/prompts/ *]
[* derive compact swarm digest from the repo danger map and shared resources *]
[* write runs/2026-04-05_174212/derived_context/swarm_digest.md *]

awdit> Swarm preflight
awdit>   Mode: repo-wide black-hat sweep
awdit>   File profile: code + config + tests
awdit>   Eligible files discovered: 6
awdit>   Token budget: 120000
awdit>   Sweep model: gpt-5.4-mini
awdit>   Proof model: gpt-5.4
awdit>   Final report style: ranked seed findings, grouped duplicates
awdit>   Repo danger map:
awdit>     `repos/tiny-notes-api_b81c2f7d/danger_map.md`
awdit>   Shared resource manifest:
awdit>     `runs/2026-04-05_174212/resources/shared/manifest.md`
awdit> Launch swarm? [Y/n]
user> y

awdit> Launching swarm batch...
[* enumerate eligible files under current scope and swarm profile *]
[* create one seed-work packet per eligible file *]
[* each seed packet includes: target file, swarm digest, shared manifest, and one-finding-max instructions *]
[* workers may inspect other repo files read-only if needed *]
[* no worker can see another worker's output *]
[* create runs/2026-04-05_174212/swarm/seeds/ *]
[* create runs/2026-04-05_174212/swarm/proofs/ *]
[* create runs/2026-04-05_174212/swarm/reports/ *]

awdit> Sweep stage started: 6 file workers queued.
[* launch worker for src/app.py *]
[* launch worker for src/auth.py *]
[* launch worker for src/routes/notes.py *]
[* launch worker for src/db.py *]
[* launch worker for tests/test_notes.py *]
[* launch worker for docker-compose.yml *]

[* worker for src/app.py inspects startup wiring and env handling *]
[* worker for src/auth.py inspects request identity derivation and debug branches *]
[* worker for src/routes/notes.py inspects note read and write paths *]
[* worker for src/db.py inspects note lookup helpers used by routes *]
[* worker for tests/test_notes.py inspects what behavior is and is not covered *]
[* worker for docker-compose.yml reports no finding *]

awdit> Sweep progress: 6/6 complete.

awdit> Example sweep outcomes:
awdit>   src/app.py
awdit>     - no finding
awdit>   src/auth.py
awdit>     - SEED-001: possible user-controlled identity fallback in debug branch
awdit>   src/routes/notes.py
awdit>     - SEED-002: note fetch path may allow cross-user read by note id
awdit>   src/db.py
awdit>     - SEED-003: note lookup helper appears unscoped by owner and may support IDOR
awdit>   tests/test_notes.py
awdit>     - SEED-004: tests cover successful note reads but not cross-user denial
awdit>   docker-compose.yml
awdit>     - no finding

[* parse all worker outputs into markdown and structured JSON *]
[* each worker output contains either zero findings or one strongest finding only *]
[* write raw seed artifacts under runs/2026-04-05_174212/swarm/seeds/ *]
[* cluster related seeds conservatively *]
[* SEED-002 and SEED-003 grouped under shared case SWM-001 *]
[* SEED-004 linked as supporting context for SWM-001, not as a promoted vulnerability seed *]
[* SEED-001 remains standalone as SWM-002 *]
[* write runs/2026-04-05_174212/swarm/reports/seed_ledger.md *]
[* write runs/2026-04-05_174212/swarm/reports/case_groups.md *]

awdit> Surviving seeds after grouping:
awdit>   - SEED-002 -> SWM-001
awdit>   - SEED-003 -> SWM-001
awdit>   - SEED-001 -> SWM-002

awdit> Launching proof stage on surviving seeds...
[* proof stage uses a stronger model than the sweep stage *]
[* proof worker for SEED-002 may inspect related files but remains attached to this seed only *]
[* proof worker for SEED-003 may inspect related files but remains attached to this seed only *]
[* proof worker for SEED-001 may inspect related files but remains attached to this seed only *]

[* proof worker for SEED-002 reads src/routes/notes.py, src/db.py, src/auth.py, and tests/test_notes.py *]
[* proof worker for SEED-002 derives concrete exploit path:
   1. authenticate as user B
   2. request GET /notes/17
   3. route checks authentication but not note ownership
   4. db helper fetches note by id only
   5. note 17 owned by user A is returned to user B
*]
[* proof worker for SEED-002 generates executable repro steps against the local app contract *]
[* record proof state: executed_proof *]

[* proof worker for SEED-003 confirms it is the same underlying issue as SEED-002 seen from a different file entrypoint *]
[* record proof state: path_grounded *]
[* mark SEED-003 as a duplicate-group member under SWM-001 *]

[* proof worker for SEED-001 reads src/auth.py and src/app.py *]
[* proof worker for SEED-001 finds that the debug fallback executes only when DEBUG=true and local-only bootstrap is enabled *]
[* no production-reachable exploit path is established from the current code *]
[* record proof state: hypothesized *]
[* SEED-001 does not meet the final report bar *]

[* write proof artifacts under runs/2026-04-05_174212/swarm/proofs/ *]
[* write per-seed proof notes, citations, and repro steps *]

awdit> Proof stage finished.

awdit> Ranked findings ready:
awdit>   1. SEED-002  case=SWM-001  state=executed_proof
awdit>      Claim: Cross-user note read via unscoped lookup by note id
awdit>      Primary file: `src/routes/notes.py`
awdit>      Related duplicate seeds: SEED-003
awdit>   filtered out:
awdit>      - SEED-001  state=hypothesized  reason=insufficient production proof

[* assemble final ranked report *]
[* keep seed findings as the ranked unit *]
[* show duplicate grouping under shared cases so the report stays organized *]
[* write runs/2026-04-05_174212/swarm/reports/final_ranked_findings.md *]
[* write runs/2026-04-05_174212/swarm/reports/final_summary.md *]
[* update run row: status=completed *]

awdit> Swarm complete.

awdit> Final artifacts
awdit>   Ranked findings:
awdit>     `runs/2026-04-05_174212/swarm/reports/final_ranked_findings.md`
awdit>   Seed ledger:
awdit>     `runs/2026-04-05_174212/swarm/reports/seed_ledger.md`
awdit>   Duplicate and case groups:
awdit>     `runs/2026-04-05_174212/swarm/reports/case_groups.md`
awdit>   Proof artifacts:
awdit>     `runs/2026-04-05_174212/swarm/proofs/`
awdit>   Shared resource manifest:
awdit>     `runs/2026-04-05_174212/resources/shared/manifest.md`
```

## Mode Summary

`awdit swarm` is a separate top-level command.
- it is not a flag on `awdit review`
- it is not the default face of the product
- it is the manual deep-audit gear for repo-wide black-hat hunting

v1 operator model:
- start the run
- let it work
- come back to the final ranked report and the raw artifacts behind it

v1 runtime model:
- read-only against the repo under review
- writes only run-scoped artifacts
- requires the repo danger-map step before launch
- reuses the current shared-resource flow
- shows one preflight confirm screen before the batch begins

This mode is intentionally simpler than the visible-slot review architecture. It does not try to mirror the hunter, skeptic, referee, and solver stage design under the hood.

## Worker Contract

Initial sweep:
- one worker per eligible file
- default eligible-file profile is code plus config and tests
- config may broaden this to all tracked files

Each worker receives:
- one seed file
- the compact swarm digest
- the shared resource manifest for the run
- explicit instructions to produce at most one strongest seed finding

Each worker may:
- inspect other repo files read-only when needed for context or proof
- cite exact file paths and code lines

Each worker may not:
- see or respond to another worker's output
- emit multiple top-level seed findings
- open a separate worker-to-worker debate thread

There are no separate neighbor-expansion workers in v1. Follow-up proof work stays attached to the surviving seed that triggered it.

## Proof And Ranking

Proof stage:
- only surviving seeds enter proof
- proof uses a stronger pass than the initial sweep
- proof may inspect nearby code and related files, but it remains attached to one seed

Final report bar:
- if executable proof is feasible, the finding should include executable repro steps
- if executable proof is not feasible, the finding must include a tight written exploit proof with exact preconditions and citations
- findings that remain merely interesting or suspicious do not belong in the final ranked report

Proof ladder for swarm outputs:
- `hypothesized`
- `path_grounded`
- `written_proof`
- `executed_proof`

Ranking rules:
- ranked unit is the seed finding, not the merged case
- duplicate and related seeds are grouped visually under shared cases
- ordering is exploitability first, then impact and confidence

This keeps the final report readable without throwing away provenance from the file-level sweep.

## Config Shape

The intended long-term config shape remains one `config/config.toml` file with clearer separation between shared, review-only, and swarm-only concerns.

Shared sections:
- `active_provider`
- `providers.<provider>`
- `scope`
- `repo_memory`
- `resources.shared`

Review-only sections:
- current slot-based sections remain the review path configuration
- `slots.<slot_name>`
- `resources.slots.<slot_name>`
- `validation.checks`

Swarm-only section:
- `swarm`

The `swarm` section should define:
- `prompt_file`
- `sweep_model`
- `proof_model`
- `eligible_file_profile`
- `token_budget`
- `allow_no_limit`

Illustrative shape:

```toml
active_provider = "openai"

[providers.openai]
api_key_env = "OPENAI_API_KEY"
base_url = "https://api.openai.com/v1"
allowed_models = ["gpt-5.4", "gpt-5.4-mini"]

[scope]
include = ["src/**", "tests/**", "config/**"]
exclude = ["docs/**", ".env", "config/resources/**"]

[repo_memory]
enabled = true
require_danger_map_approval = true
confirm_refresh_on_startup = true
auto_update_on_completion = true

[resources.shared]
include = ["docs/architecture.md"]
exclude = []

# Review-only path
[slots.hunter_1]
default_model = "gpt-5.4-mini"
reasoning_effort = "low"
prompt_file = "prompts/hunter_1.md"

# Swarm-only path
[swarm]
prompt_file = "prompts/swarm.md"
sweep_model = "gpt-5.4-mini"
proof_model = "gpt-5.4"
eligible_file_profile = "code_config_tests"
token_budget = 120000
allow_no_limit = true
```

The confirm screen for `awdit swarm` should surface:
- the eligible-file profile
- the discovered eligible file count
- the token budget, or that the run is explicitly no-limit
- the selected sweep and proof models
- the danger-map path
- the shared resource manifest path

## Run Artifacts

Swarm mode should keep its run-local artifacts under `runs/<run_id>/swarm/`.

Expected directories:

```text
runs/<run_id>/
  prompts/
  derived_context/
    swarm_digest.md
  resources/
    shared/
      manifest.md
  swarm/
    seeds/
    proofs/
    reports/
```

Artifact families:
- `swarm/seeds/`
  - raw markdown and structured JSON output for each seed worker
- `swarm/proofs/`
  - proof notes, exploit steps, citations, and any executable repro artifacts
- `swarm/reports/seed_ledger.md`
  - all initial seeds and zero-finding outcomes
- `swarm/reports/case_groups.md`
  - duplicate and related-seed grouping under shared case IDs
- `swarm/reports/final_ranked_findings.md`
  - the main forward-facing ranked report
- `swarm/reports/final_summary.md`
  - short run summary with top finding links and filtering notes

The final operator-facing object is `final_ranked_findings.md`. Everything else exists to preserve traceability, proof, and grouping logic behind that report.

# Awdit: AI Security Audit Research And Proposal

Date: 2026-03-27

This document is the final output of a two-round research pass on how AI agents should perform security audits.

- Round one used parallel GPT-5.4 medium researchers plus direct source review to map what exists, what has worked, and what adjacent systems teach us.
- Round two used a fresh parallel challenger wave to attack the round-one thesis and only keep changes where the new evidence was genuinely better.

Raw notes and intermediate memos live under `research/`.

## Repo Context

`awdit` already points in a promising direction: competing hunters, skeptics, referees, and solvers; artifact-heavy runs; and explicit human truth review. The right question was never "should AI help AppSec?" It was "what product shape actually earns trust while still discovering things scanners miss?"

## Round One: What Exists, What Works, What Is Missing

### 1. The strongest shipped products are grounded and narrow

The most credible current systems do not ask a model to freestyle across an entire repo and call it security review.

- GitHub Copilot Autofix is analyzer-first: CodeQL creates the issue anchor, then the model proposes a patch from bounded context.
- GitLab splits explanation, likely-false-positive handling, and remediation into separate jobs, and only gets more agentic after triage already exists.
- Semgrep Assistant is strongest as triage infrastructure: explanation, suppression, prioritization, project memory, and feedback loops.
- Snyk's public architecture points toward the same conclusion: symbolic or structural analysis should constrain generation, not compete with it.

Round-one conclusion: the best production systems trust the model with a smaller job than the marketing implies.

### 2. The frontier is shifting toward "security researcher" behavior

The more interesting frontier systems do not start from "what rule fired?" They start from "what attack paths are realistic here?"

- Codex Security says it builds a codebase-specific threat model, scans repository history, validates issues in isolation, and only then proposes a patch.
- Project Naptime and Big Sleep show the same strategic move from a research angle: tool-using agents get much stronger when they can browse code, form hypotheses, run validators, and iterate like a human vulnerability researcher.

Round-one conclusion: the real opportunity is not better alert explanation. It is AI that can do disciplined investigation.

### 3. Adjacent systems often have better product instincts than AI security tools

Some of the best design lessons came from outside the current AI AppSec product category.

- OSS-Fuzz, CIFuzz, and ClusterFuzzLite optimize for reproducibility, novelty, and deduplication before they interrupt humans.
- CodeQL multi-repository variant analysis exists because serious security work does not end with one instance of one bug.
- Bug bounty and audit platforms care deeply about duplicate collapse, explicit states, provenance, adjudication, and fairness.
- Fix-linked datasets such as CVEfixes are more useful for repair systems than vulnerability labels alone because they preserve how real fixes were actually shipped.

Round-one conclusion: queue hygiene, provenance, and reproducibility are product primitives, not polish.

### 4. Repair and evaluation research points to a harder truth

The most important warning from the research side is that a plausible patch is not the same thing as a good fix.

- AutoPatchBench shows that "build succeeds and the symptom goes away" badly overstates fix quality.
- Program-repair literature has the same long-running result: test-suite success alone often rewards overfitting.
- Research on neuro-symbolic vulnerability discovery and repair keeps pointing in the same direction: structure narrows the search space; the model reasons inside it.

Round-one conclusion: the right architecture is not scanner-only and not freeform-agent-only. It is neuro-symbolic and validator-heavy.

## Round-One Synthesis

The best ideas from round one were:

- Threat model first.
- Case file or issue packet as the primary object.
- Structured narrowing before open-ended reasoning.
- Separate discovery, adjudication, and remediation.
- Noise suppression as core value.
- Explicit provenance and exact code references.
- Multi-layer validation instead of one green check.
- Variant analysis and repair memory, not just per-issue patching.
- Human truth review for the issues that survive machine skepticism.

The big gaps across the market were:

- Most tools still begin after a scanner alert.
- Most reason per finding, not per attack path.
- Few show why a weak claim died before it reached the user.
- Few distinguish evidence states cleanly.
- Few build local security memory.
- Few close the loop with invariant capture and variant scans.
- Most UX collapses into inline comment spam or another alert dashboard.

## The Missing Shape

What seems mostly unbuilt, even now, is a calm security investigator with the following properties:

- it reasons in attack paths and trust boundaries, not only alert rows
- it creates a small number of canonical case files instead of many scattered comments
- it applies internal skepticism before asking for human time
- it records why weak branches were suppressed
- it distinguishes "reasoned," "reproduced," "behavior-checked," and "variant-scanned"
- it learns local security invariants over time
- it closes fixes at the bug-family level, not just the demo exploit

That is the opening for `awdit`.

## Opinionated Proposal After Round One

Round one pushed toward a bold thesis:

`awdit` should not be a scanner with more chat. It should be a threat-model-driven security investigation engine.

The rough shape looked like this:

1. Build an editable threat model of entry points, trust boundaries, sensitive data, and high-impact flows.
2. Use code structure, diffs, optional scanner evidence, and repo context to narrow the search space.
3. Let hunters generate candidate exploit stories inside that narrowed space.
4. Collapse overlaps early into canonical case files.
5. Let skeptics try to kill weak claims.
6. Only promote surviving cases to a human truth-review queue.
7. Generate fixes only after the finding is strong enough to deserve repair work.
8. Attach validation artifacts and variant analysis before calling the loop complete.

This was directionally right, but round two forced important corrections.

## Round Two: What Challenged The First Thesis

Round two did not ask for more supporting examples. It asked for contradictions, stronger alternatives, and operational failure modes.

### Challenge 1: Simpler, more structured systems often outperform freer agentic systems

The strongest contradiction to the round-one vision was not "agents are bad." It was "unconstrained agents are worse than structured hybrids."

- GitHub's review products are purpose-built and tuned for consistency, not exposed as arbitrary model playgrounds.
- Neuro-symbolic work such as MoCQ and VERCATION points toward a stronger pattern: use structure to carve out candidate space, then let the model reason inside it.

What changed:

- `awdit` should use conditional specialists, not a permanent visible swarm.
- Structural narrowing should happen before most reasoning.
- Default stage behavior should be curated and stable; model freedom should be secondary.

### Challenge 2: Visible multi-agent meshes are a trust and safety liability

Round one liked adversarial roles. Round two found a real caution: multi-agent systems add leakage and control risk when handoffs become freeform.

- AgentLeak and related multi-agent safety work suggest that more internal channels can create more exposure, not less.

What changed:

- Keep adversarial pressure, but make handoffs sparse, typed, logged, and redactable.
- Prefer one evidence ledger and one case-file schema over open-ended internal chat.

### Challenge 3: Confidence scores and LLM judges are weaker than they look

Round one still leaned on adjudication and confidence language. Round two forced more humility.

- Semgrep's metrics docs are careful about what their numbers mean; many users will still overread them.
- GitLab exposes confidence but does not pretend it eliminates review.
- Preference Leakage and contamination-limited benchmark work make judge-only evaluation look fragile.

What changed:

- No single magic confidence score for a whole finding.
- Confidence should attach to specific decisions: duplicate collapse, exploit completeness, variant similarity, behavior-preservation risk.
- Critical quality claims should come from validator artifacts and human truth review, not same-family model judgment alone.

### Challenge 4: The fix loop must be stricter than round one allowed

Round one was right to separate truth from repair. It was still too generous to exploit reproduction and passing tests.

- AutoPatchBench is the clearest warning that naive validation wildly overstates repair quality.
- GitHub's responsible-use guidance is explicit about partial fixes and semantic regressions.
- Patch-oriented research suggests that stronger repair systems use invariants, patterns, and structural constraints, not just fluent patch generation.
- Project Zero's incomplete-fix lessons make variant closure non-optional.

What changed:

- Reproduction is one rung on a proof ladder, not the end.
- Every serious patch should state the invariant it claims to restore.
- Variant analysis belongs inside the remediation loop, not after it.

### Challenge 5: The launch wedge has to be narrower than the grand vision

The biggest product correction from round two was scope.

- Fast-path review systems win because they are narrow, timely, and quiet.
- Codex Security explicitly recommends starting with a small repo set and a dedicated reviewer group.
- CIFuzz-style workflows are effective because they fit existing developer timing.

What changed:

- `awdit` should launch diff-first and subsystem-first.
- Deep whole-repo audit should be a scheduled mode, not the default identity.
- Continuous full-repo autonomy should come much later, if at all.

## What Survived Round Two

The following ideas survived the challenge pass and still look like the right core:

- threat-model-first narrowing
- attack-path-native reasoning
- canonical case files instead of alert rows
- internal skepticism before human interruption
- typed evidence handoffs
- multi-family validation
- local security memory
- variant-aware closure

The following ideas changed materially:

- from visible swarm to hidden conditional specialists
- from freeform agentic search to neuro-symbolic search inside narrowed spaces
- from one confidence score to scoped evidence states
- from "validated exploit plus tests" to invariant-first, variant-aware fix closure
- from broad launch ambition to a narrower diff-first wedge

## Final Proposal For awdit

### Product thesis

`awdit` should feel like a quiet security investigator, not a scanner console and not a chatbot.

The visible product should be simple:

- choose a target
- inspect or edit the threat model
- review a short queue of case files
- inspect evidence and rebuttals when needed
- choose or approve a fix only after the case is strong

The internal machinery can be sophisticated, but the user should mostly experience calm.

### Primary object: the case file

The canonical object in the system should be a case file, not an alert and not a comment thread.

Each case file should contain:

- stable case ID
- exact code references
- attacker-controlled source or precondition
- trust boundary crossed
- sensitive sink or outcome
- exploit story
- strongest evidence for the claim
- strongest evidence against the claim
- overlap and duplicate relations
- validator-family results
- invariant that appears broken
- fixability estimate
- interruption recommendation
- variant-scan summary

That one object should drive both human review and solver work.

### Architecture

The internal architecture should look like this:

1. Threat-model mapper
   Builds and updates a compact model of trust boundaries, critical assets, invariants, and hot paths.
2. Structural narrows
   Uses diff scope, call-graph hints, slices, schema/config links, git history, and optional scanner evidence to reduce the search space.
3. Conditional specialists
   Launches targeted hunters only where the narrowed space suggests real leverage.
4. Skeptic gate
   Tries to kill weak branches before they become user-facing work.
5. Case-file promoter
   Converts only surviving branches into canonical case files.
6. Human truth queue
   Keeps the reviewer focused on a small number of high-signal cases.
7. Repair loop
   Generates narrow candidate patches constrained by invariants, repair patterns, and repo-specific norms.
8. Variant closure
   Searches for sibling instances before calling the issue closed.
9. Memory writer
   Updates security memory from truth labels, failed validators, and accepted fixes.

### Product modes

`awdit` should ship with three clear gears:

1. Diff review
   The default launch wedge. Fast, narrow, quiet, integrated with PR or commit review.
2. Hot-path audit
   For a subsystem, surface area, or security-critical boundary.
3. Scheduled deep audit
   A slower background mode for wider investigation and memory building.

Do not make full-repo always-on deep audit the default story.

### Evidence ladder

Every case file and every fix should carry explicit evidence states. Suggested ladder:

- `hypothesized`
- `path-grounded`
- `reproduced`
- `invariant-defined`
- `patch-reanalyzed`
- `regression-checked`
- `behavior-checked`
- `variant-scanned`
- `human-confirmed`

This is better than one confidence score because each rung means something different and catches different failure modes.

### Repair loop

The repair loop should be stricter than most current AI security tools:

1. confirm the case is strong enough to deserve repair work
2. state the broken invariant explicitly
3. generate one or more narrow patches, constrained by repair patterns and local style
4. re-run static or structural analysis on the modified code
5. replay the exploit or trigger path when possible
6. run stronger checks for overfitting or regression
7. run a nearby variant scan and attach the result
8. surface the patch with remaining uncertainty, not false finality

The goal is not just "patch generated." The goal is "class of bug more plausibly closed."

### Memory

`awdit` should keep security memory, not chat memory.

It should remember:

- prior true and false positives
- accepted and rejected exploit shapes
- local authn/authz and data-flow invariants
- trusted sanitizers and dangerous sinks
- successful repair patterns
- validators that caught false confidence
- subsystems that repeatedly produce review pain

This should behave like a living security notebook for the repo.

### Evaluation

The system should optimize for operational truth, not benchmark theater.

The most important metrics are:

- interrupt precision: how often a surfaced case was worth the human's time
- duplicate collapse quality
- exploit reproduction rate on promoted cases
- fix survival after stronger validators
- variant yield after first discovery
- reviewer acceptance and re-open rate
- memory usefulness: did previous truth labels reduce future noise
- time-to-high-confidence case file

Use human truth review and validator artifacts as primary signals. Use model judges only as secondary support.

## What Nobody Seems To Have Tried Clearly Enough Yet

The best unexplored combination looks like this:

- threat-model-first repo understanding
- neuro-symbolic narrowing before reasoning
- hidden specialists with typed handoffs
- canonical case files instead of alert floods
- aggressive suppression of weak branches before human review
- invariant-first, variant-aware fix closure
- local security memory that compounds from real truth labels

In other words: not "more agents than everyone else," but a calmer and more disciplined security investigator than anyone has shipped.

## Bottom Line

If `awdit` tries to be a universal autonomous audit swarm on day one, it will likely fail on usability and trust before it fails on raw model ability.

If `awdit` launches as a diff-first, case-file-centric, threat-model-driven investigator with hidden conditional specialists, typed evidence handoffs, and a strict evidence ladder, it has a real chance to be both simpler and more accurate than the current market.

That is the opinionated proposal this research supports.

## Sources

- [OpenAI Help Center: Codex Security](https://help.openai.com/en/articles/20001107-codex-security)
- [GitHub Docs: Copilot Autofix for code scanning](https://docs.github.com/en/code-security/concepts/code-scanning/copilot-autofix-for-code-scanning)
- [GitHub Docs: Responsible use of Copilot Autofix](https://docs.github.com/en/code-security/responsible-use/responsible-use-autofix-code-scanning)
- [GitHub Docs: Copilot code review](https://docs.github.com/copilot/concepts/code-review)
- [GitHub Docs: Responsible use of Copilot code review](https://docs.github.com/en/copilot/responsible-use/code-review)
- [GitHub Docs: Multi-repository variant analysis](https://docs.github.com/en/code-security/concepts/code-scanning/multi-repository-variant-analysis)
- [GitLab Docs: Explain vulnerabilities with AI](https://docs.gitlab.com/user/application_security/analyze/duo/)
- [GitLab Docs: False positive detection](https://docs.gitlab.com/user/application_security/vulnerabilities/false_positive_detection/)
- [GitLab Docs: Agentic SAST Vulnerability Resolution](https://docs.gitlab.com/user/application_security/vulnerabilities/agentic_vulnerability_resolution/)
- [Semgrep Docs: Assistant overview](https://semgrep.dev/docs/semgrep-assistant/overview)
- [Semgrep Docs: Assistant metrics and methodology](https://semgrep.dev/docs/semgrep-assistant/metrics)
- [Snyk: DeepCode AI](https://snyk.io/platform/deepcode-ai/)
- [Project Zero: Project Naptime](https://projectzero.google/2024/06/project-naptime.html)
- [Project Zero: From Naptime to Big Sleep](https://googleprojectzero.blogspot.com/2024/10/from-naptime-to-big-sleep.html)
- [Google OSS-Fuzz: CIFuzz](https://google.github.io/oss-fuzz/getting-started/continuous-integration/)
- [Google ClusterFuzzLite](https://google.github.io/clusterfuzzlite/)
- [HackerOne Docs: Duplicate reports](https://docs.hackerone.com/en/articles/8514410-duplicate-reports)
- [HackerOne Docs: Report states](https://docs.hackerone.com/en/articles/8475030-report-states)
- [Meta Engineering: AutoPatchBench](https://engineering.fb.com/2025/04/29/ai-research/autopatchbench-benchmark-ai-powered-security-fixes/)
- [Zenodo: CVEfixes Dataset](https://zenodo.org/records/7029359)
- [MoCQ / arXiv 2504.16057](https://www.emergentmind.com/papers/2504.16057)
- [VERCATION publication record](https://research.monash.edu/en/publications/vercation-precise-vulnerable-open-source-software-version-identif/)
- [Preference Leakage / arXiv 2502.01534 record](https://dblp.org/rec/journals/corr/abs-2502-01534)
- [LiveBench: contamination-limited evaluation](https://openreview.net/forum?id=sKYHBTAxVa)
- [AgentLeak / arXiv 2602.11510 mirror](https://huggingface.co/papers/2602.11510)

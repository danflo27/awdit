# Round One Synthesis: AI Security Audit Systems, Gaps, and a Proposal for Braind

## What round one looked at

Round one combined:

- direct-product research on scanner-plus-AI systems
- research on agentic security systems and vulnerability benchmarks
- adjacent patterns from real audit practice, variant analysis, and triage-heavy AppSec workflows
- one delegated subagent memo in [`research/round1/agent-a-direct-landscape.md`](/Users/df/projects/awdit/research/round1/agent-a-direct-landscape.md)

The goal here is not to produce a neutral market summary. It is to identify what is genuinely worth stealing, what is overfit to vendor constraints, and what opens a path to a stronger product.

## What exists now

### 1. Scanner-grounded AI remediation is the dominant shape

The most mature commercial systems still begin with a traditional analyzer or finding source, then use AI to explain, suppress, prioritize, or patch.

- GitHub Copilot Autofix starts from CodeQL alerts, uses alert metadata plus nearby code context, and validates suggested patches with an internal harness before surfacing them.
- GitLab Duo and Agentic SAST Resolution stage the workflow as detection, explanation, likely-false-positive analysis, and then iterative remediation.
- Semgrep Assistant is strongest when acting as triage infrastructure: explanation, noise filtering, project memory, and interruption management.
- Snyk's public architecture points toward a hybrid symbolic-plus-generative design, with analyzers constraining generation and only some issue classes marked autofixable.

Why this matters:

- these systems are practical because they are grounded
- they fit existing workflows well
- they still inherit the blind spots of the upstream analyzer
- they mostly reason per finding, not per attack path or exploit chain

## What seems strongest in current implementations

### Threat-model-first reasoning

OpenAI's Aardvark, now Codex Security, is the clearest public example of a system that is trying to behave like a security researcher rather than an alert explainer. It builds a codebase-specific threat model, scans repository history and new commits, validates candidate issues in an isolated environment, and only then proposes a minimal patch for human review.

That is a major move.

It suggests the strongest agentic systems will not begin with "what lint rule fired?" They will begin with "what are the trust boundaries, attacker entry points, and sensitive outcomes in this codebase?"

### Validation before interruption

The highest-trust systems all put some gate between speculative reasoning and user interruption:

- GitHub talks about validation and quality monitoring for fixes.
- GitLab validates remediation through pipeline or test workflows.
- Codex Security attempts sandboxed reproduction before surfacing findings.
- Real human audit ecosystems like Code4rena strongly reward clear reproduction steps and concrete impact.

The pattern is simple: raw AI suspicion is cheap; human attention is expensive.

### Triage quality is product quality

Semgrep Assistant gets this more clearly than most. The system is not valuable just because it can generate explanations. It is valuable because it reduces noise, learns from prior triage, and decides which findings should interrupt humans.

The best AppSec products increasingly compete on signal management, not just raw recall.

### Minimal patches earn trust, but root-cause thinking matters

Both GitHub Autofix and Codex Security emphasize minimal patches. That is a good trust pattern because narrow fixes are easier to review and less likely to cause collateral damage.

But minimal patches can also be too local. Security bugs often reflect missing invariants, not one broken line. The right pattern is:

- prefer a narrow fix first
- escalate to a wider fix only when the validator or reviewer can show the local patch is incomplete

## What everything important still misses

### 1. Very few systems audit like an adversarial team

Most products are still linear:

- detect
- explain
- maybe suppress
- maybe patch

What is missing is adversarial pressure inside the machine:

- a hunter that is allowed to overgenerate
- a challenger that tries hard to kill weak hypotheses
- a validator that demands proof or near-proof
- a synthesizer that explains what survived and why

That is exactly where `awdit` is already directionally right.

### 2. Most systems think in alerts, not attack paths

Security analysts do not really think in isolated findings. They think in:

- attacker entry point
- trust boundary crossed
- capability gained
- sensitive action reached
- exploit preconditions
- realistic impact

Vendors still mostly package work as single findings. That makes it hard to see exploit chains, variant classes, or architectural root causes.

### 3. Audit memory is weak

The tools increasingly store suppression feedback, but they do not yet build a rich, editable memory of:

- project-specific security invariants
- previously rejected hypothesis classes
- accepted exploit patterns
- recurring risky subsystems
- fix patterns that regress later

That means teams keep retraining the system through repeated review instead of compounding understanding.

### 4. Nobody is really solving "proof of absence"

Systems can list findings, but they are still weak at telling a human:

- what areas were investigated deeply
- what threat classes were considered and ruled out
- where confidence is low because the system lacked runtime access, missing configs, or ambiguous assumptions

That makes them feel magical when they hit and slippery when they miss.

## Adjacent lessons worth stealing

### Variant analysis, not just bug reports

Project Zero's writeups repeatedly show that incomplete fixes and regressions create variants. A good system should not stop at "fix this bug." It should ask:

- what family of mistakes produced this bug?
- where else does the same broken assumption appear?
- can the proposed fix regress later during refactor?

### Competitive auditing improves recall

Code4rena and similar audit competitions reward independent discovery and strong reproduction. Multiple parallel thinkers beat one monolithic reviewer because they search differently and phrase the problem differently.

### Good benchmarks optimize the right tradeoff

PrimeVul's framing is helpful because it explicitly cares about the tradeoff between missed vulnerabilities and developer overwhelm, rather than accuracy in the abstract. That is closer to product reality than a generic classification score.

### Datasets with fixes matter more than vulnerability labels alone

CVEfixes is useful because it ties vulnerabilities to real-world fixes across commits, files, and functions. If you want a system that learns how security bugs are actually repaired, you need fix-linked data, not just vulnerable snippets.

## Opinionated proposal: how Braind should work

Braind should not present itself as a swarm of named agents. That is an implementation detail. The user should experience one security researcher with receipts.

Under the hood, Braind can still use multiple internal roles, but the product should stay simple.

## The core idea

Braind should be a threat-model-driven audit engine that reasons in attack paths, validates before interrupting, and learns from human truth review.

Not "a scanner with an LLM on top."

Not "a chat UI that sort of reviews code."

A better mental model is:

- mapper
- hunter
- challenger
- validator
- fixer

But those roles are mostly hidden from the user.

## A concrete product shape

### 1. Start every audit by building an editable threat model

Before hunting for bugs, Braind should build a project-specific model of:

- entry points
- identities and trust boundaries
- sensitive data
- state transitions
- dangerous side effects
- high-risk subsystems
- expected security invariants

Then show it to the user as a compact editable artifact, not a wall of prose.

This is the biggest missing primitive in current tools.

### 2. Hunt in attack paths, not isolated alerts

Each candidate should be represented as an attack path card:

- attacker-controlled source
- intermediate path
- boundary crossed
- sensitive sink or outcome
- exploit conditions
- evidence
- affected files and lines
- confidence
- validation status

This becomes the central object in the system, replacing the weaker "finding row" model.

### 3. Use adversarial compression before human review

Every attack-path candidate should go through:

- generation by one or more hunters
- direct attack from a challenger that tries to break the claim
- validation attempts if the claim survives
- synthesis into one issue packet

The human should review the compressed result, not the raw debate, unless they expand it.

### 4. Make validation a ladder, not a binary checkbox

Braind should track validation stages explicitly:

- `reasoned`: plausible from static evidence
- `grounded`: backed by exact code path and invariant analysis
- `reproduced`: triggered in a harness or sandbox
- `regression-tested`: fix includes proof it blocks the exploit path
- `variant-checked`: related instances searched and summarized

This is much better than one generic confidence score.

### 5. Learn from truth review and suppression

Every human decision should write memory back into the system:

- confirmed exploit pattern
- false-positive pattern
- project-specific invariant
- accepted risk
- preferred fix style
- subsystems that need deeper future scrutiny

The system should become locally smart, not just globally pretrained.

### 6. Keep the visible UX brutally simple

Do not expose eight agent slots as the primary product concept.

Expose:

- target
- scope
- threat model
- findings queue
- proof artifacts
- fix options
- final report

Advanced users can inspect prompts, internal roles, and raw debate, but that should be secondary.

### 7. Fixes should compete, but only after truth is established

The current `awdit` architecture is right to separate "is this real?" from "how should we fix it?"

Braind should preserve that separation and maybe simplify it:

- one proof-backed issue packet
- two fix strategies when the issue is real: minimal patch and structural patch
- automatic validation and revalidation
- user chooses with the validator results visible

This is a better abstraction than exposing solver personalities.

## What I would change in the current awdit direction

The repo's current architecture is directionally strong, but for usability and simplicity I would change several things:

### Hide agent slots in the main UX

Keep adversarial roles internally, but do not make the user manually configure eight named slots in the normal happy path. Offer:

- `fast audit`
- `deep audit`
- `patch mode`

Each mode can map to different internal role topologies and model budgets.

### Add a visible threat-model stage before hunters

Right now the architecture mentions context gathering and external resources, but the strongest modern pattern is an explicit, inspectable threat model. That should become a first-class artifact.

### Replace "finding" as the center of gravity with "issue packet"

The issue packet should be richer and path-based from the start. It should carry:

- attack path
- violated invariant
- exploit conditions
- validation state
- challenge transcript summary
- fixability and verifiability hints

### Add variant search after validation

When an issue is validated, the system should automatically run a focused variant pass over adjacent code and related patterns. This is a direct lift from high-end human security work.

### Reduce visible complexity while increasing internal rigor

Braind should feel simpler than current tools, not more exotic:

- one clean audit workspace
- one queue of issue packets
- one truth review flow
- one remediation flow

The magic happens behind the glass.

## The strongest thesis from round one

The future is not "more AI in scanning."

The future is a system that:

- builds its own threat model
- hunts creatively across real code context
- forces itself to survive internal skepticism
- validates before interrupting people
- stores project-specific security memory
- treats fixes as a separate, evidence-backed step

That is the lane where Braind can actually be differentiated.

## Sources

- [OpenAI: Introducing Aardvark](https://openai.com/index/introducing-aardvark/) (October 30, 2025; updated March 6, 2026)
- [OpenAI Help Center: Codex Security](https://help.openai.com/en/articles/20001107-codex-security) (updated March 2026)
- [GitHub Docs: Copilot Autofix for code scanning](https://docs.github.com/en/code-security/code-scanning/managing-code-scanning-alerts/about-autofix-for-codeql-code-scanning)
- [GitLab Docs: Explain vulnerabilities with AI](https://docs.gitlab.com/user/application_security/analyze/duo/)
- [GitLab Docs: Agentic SAST Vulnerability Resolution](https://docs.gitlab.com/user/application_security/vulnerabilities/agentic_vulnerability_resolution/)
- [Semgrep Assistant overview](https://semgrep.dev/docs/semgrep-assistant/overview)
- [Semgrep Assistant analyze findings](https://semgrep.dev/docs/semgrep-assistant/analyze)
- [Semgrep Assistant metrics](https://semgrep.dev/docs/semgrep-assistant/metrics)
- [Snyk DeepCode AI](https://snyk.io/platform/deepcode-ai/)
- [Snyk: Fix code vulnerabilities automatically](https://docs.snyk.io/scan-with-snyk/snyk-code/manage-code-vulnerabilities/fix-code-vulnerabilities-automatically)
- [Code4rena submission guidelines](https://docs.code4rena.com/competitions/submission-guidelines)
- [Project Zero: An Autopsy on a Zombie In-the-Wild 0-day](https://googleprojectzero.blogspot.com/2022/06/an-autopsy-on-zombie-in-wild-0-day.html)
- [PrimeVul dataset repository](https://github.com/DLVulDet/PrimeVul)
- [CVEfixes repository](https://github.com/secureIT-project/CVEfixes)

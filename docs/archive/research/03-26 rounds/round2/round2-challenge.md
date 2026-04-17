# Round Two Challenge Pass: What Breaks Round One, What Survives, and What Changes

Round one favored a threat-model-driven, adversarial audit engine. Round two tried to break that thesis by looking for evidence that:

- analyzer-grounded systems outperform freeform agents
- multi-agent architectures create new problems
- evaluation claims are weaker than they look
- simpler workflows beat more ambitious orchestration

This pass did change the proposal.

## Challenge 1: The best narrow systems are more neuro-symbolic than agentic

Round one leaned toward "security researcher in a box." Round two found strong evidence that the best-performing narrow systems often win by giving the model a smaller, more structured job.

### Stronger-than-expected evidence

- VERCATION combines program slicing, commit backtracking, and AST-based clone detection with an LLM to identify vulnerable OSS versions. Reported result: 93.1% F1 on a curated dataset and 202 incorrect vulnerable-version records found in NVD.
- MoCQ uses an LLM to synthesize vulnerability queries, then refines and executes them through static analysis. It reportedly found 12 missed patterns and 7 unknown real-world vulnerabilities.
- GPTVD and similar slice-based work report that explicit static slices plus curated reasoning examples materially improve precision and interpretability over raw code prompting.

### What this means

The challenge is not "static analysis or agents." The stronger frame is:

- use structural analysis to constrain the search space
- use model reasoning to generate and rank hypotheses inside that space
- use validators to confirm or kill the best candidates

### Direct comparison to round one

Round one was right that pure scanner-plus-autofix is too weak. But it underweighted how much deterministic scaffolding is needed before agentic reasoning becomes reliable.

### Proposal update

Braind should not be a pure freeform repo explorer. It should be a threat-model-driven, neuro-symbolic auditor:

- threat model narrows the hunt
- graph, slice, and diff structure narrow the evidence
- agents reason over structured candidate spaces

## Challenge 2: Multi-agent systems can make privacy and control worse

Round one liked adversarial roles. Round two found a serious counterweight: multi-agent systems create additional internal attack surfaces and observability problems.

### Stronger-than-expected evidence

- AgentLeak reports that multi-agent systems can lower output-channel leakage while increasing total exposure through inter-agent communication and shared internal channels.
- The broader lesson is not limited to privacy. Every extra agent hop creates one more place for hidden assumptions, prompt injection, or data spillage to accumulate.

### What this means

If Braind uses multiple agents, the handoffs must be:

- sparse
- typed
- logged
- redactable

Freeform internal agent conversations are a liability.

### Direct comparison to round one

Round one celebrated internal adversarial pressure. That survives. What changes is the transport layer: structured baton passing beats open-ended group chat.

### Proposal update

Braind should use:

- one shared evidence ledger
- one typed issue-packet schema
- one small number of role transitions

Not a sprawling multi-agent conversation mesh.

## Challenge 3: LLM-as-judge can quietly poison evaluation

Round one relied heavily on adjudication and truth review concepts. Round two found evidence that naive LLM judging is much riskier than it first appears.

### Stronger-than-expected evidence

- Preference Leakage shows that related generator and judge models can bias evaluation results.
- Benchmark contamination work more broadly suggests many headline benchmark numbers are less trustworthy than they appear when overlap or memorization is plausible.

### What this means

Braind should not evaluate itself mainly with:

- same-family model judges
- synthetic findings graded by related models
- frozen canned benchmarks only

### Direct comparison to round one

Round one wanted a referee/synthesis layer. That survives, but the judge must not be treated as ground truth.

### Proposal update

Braind evaluation should rely on:

- blinded human truth review for critical samples
- exploit success and fix revalidation as primary evidence
- cross-family or rule-based judge diversity only as secondary signal

## Challenge 4: Threat-model-driven systems are promising, but rollout has to be narrower than the grand vision

Round one wanted a bold, full audit engine. Round two found evidence that the strongest real systems still recommend constrained rollout and small-scope adoption first.

### Stronger-than-expected evidence

- Codex Security explicitly recommends starting with a small set of repositories and a dedicated reviewer group.
- GitHub and GitLab both keep AI tightly inside existing workflows and high-signal cases rather than making it the universal first reviewer for everything.

### What this means

A system that tries to do whole-repo, whole-org, always-on, exploit-validating auditing from day one is likely to fail on usability before it fails on model quality.

### Direct comparison to round one

Round one was too willing to make whole-repo deep audit the main identity. The better wedge is narrower.

### Proposal update

Braind should launch in this order:

- PR and diff mode
- hot-path subsystem mode
- scheduled deep audits
- only later, full repo continuous mode

## Challenge 5: The best contradiction to "minimal fixes" is not bigger fixes, but variant-aware fixes

Round one argued against overly local remediation. Round two sharpened that.

Project Zero's variant-analysis lessons suggest the main risk is not that minimal patches are small. It is that they are unaccompanied by:

- invariant capture
- regression tests
- nearby variant search

### Proposal update

Keep minimal patches as the default recommendation, but require Braind to attach:

- the invariant the patch intends to restore
- a test or validator artifact
- a variant scan summary

That gets most of the trust benefits of small patches without pretending local fixes are always enough.

## What changed and why

### Change 1: from "agentic" to "neuro-symbolic agentic"

Why:

- MoCQ and VERCATION are better evidence than freeform-agent hype
- structured narrowing seems to be where reliability comes from

### Change 2: from "many agent roles" to "few typed handoffs"

Why:

- AgentLeak makes uncontrolled inter-agent communication look dangerous
- usability also improves when internal complexity is hidden

### Change 3: from "referee confidence" to "evidence ladder plus human truth"

Why:

- preference leakage and benchmark contamination weaken judge-only evaluation
- exploitability and revalidation are harder signals

### Change 4: from "whole-repo audit product" to "progressive rollout product"

Why:

- the strongest live systems are careful about scope and onboarding
- tight first use cases will teach the system faster and create less cognitive drag

## Revised thesis after round two

Braind should be:

- threat-model-driven
- neuro-symbolically grounded
- adversarial internally but with sparse typed handoffs
- validation-heavy
- human-truth-calibrated
- narrow at launch, not grandiose

That is stronger than round one because it keeps the ambition while removing the parts most likely to become unreliable theater.

## Sources

- [OpenAI Help Center: Codex Security](https://help.openai.com/en/articles/20001107-codex-security) (updated March 2026)
- [VERCATION publication record](https://research.monash.edu/en/publications/vercation-precise-vulnerable-open-source-software-version-identif/) (IEEE TSE, February 2026)
- [MoCQ paper record](https://dblp.org/rec/journals/corr/abs-2504-16057) (arXiv 2504.16057, April 2025)
- [Preference Leakage paper record](https://dblp.org/rec/journals/corr/abs-2502-01534) (arXiv 2502.01534, February 2025)
- [AgentLeak paper page](https://huggingface.co/papers/2602.11510) (February 2026)

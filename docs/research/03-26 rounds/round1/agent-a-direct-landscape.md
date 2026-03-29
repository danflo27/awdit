# Round One Research A: Direct Landscape of AI Security Audit Systems

## Framing

This memo looks at concrete systems that already use AI to analyze, triage, explain, or remediate security issues in code. I treated each one as raw material for a future product called `braind`/`awdit`, not as something to copy wholesale.

Repo context note: `awdit` already proposes a hunter -> skeptic -> referee -> solver pipeline in [docs/architecture.md](/Users/df/projects/awdit/docs/architecture.md). The most useful question is not "who has AI in AppSec?" but "what product and systems patterns are already working, and where are they still thin?"

## Finding 1: GitHub Copilot Autofix is analyzer-first, patch-second

### What exists

GitHub’s security flow is still anchored in CodeQL. Copilot Autofix sits downstream of a code-scanning alert and turns the alert plus local code context into a suggested patch. GitHub says Autofix uses the alert description and location, branch code, nearby snippets around source and sink locations, short file headers, and CodeQL help text to generate the fix. It also says every suggestion is tested before it is shown, and that it continuously monitors quality with an internal harness of more than 2,300 alerts from public repositories with test coverage.

Sources:
- [GitHub Docs: About Copilot Autofix for code scanning](https://docs.github.com/en/code-security/concepts/code-scanning/copilot-autofix-for-code-scanning)
- [GitHub Docs: Responsible use of Copilot Autofix for code scanning](https://docs.github.com/en/code-security/responsible-use/responsible-use-autofix-code-scanning)

### Mechanism worth noticing

- Static analysis creates a precise vulnerability anchor first.
- The LLM is constrained by structured alert data instead of being asked to freeform hunt the whole repo.
- GitHub explicitly admits nondeterminism, context truncation, partial fixes, semantic regressions, and the risk of introducing new vulnerabilities.
- It uses automated testing as a gate before surfacing a patch.

### Strengths

- Excellent trust posture: the system is honest about limits.
- Strong developer ergonomics: the suggestion appears directly inside existing code-scanning workflows.
- The patch is attached to a known alert, which keeps the problem legible.

### Weaknesses

- Hunting is weak because the model does not originate the issue; it inherits the scanner’s worldview.
- The context window is intentionally narrow, which makes subtle multi-file logic bugs hard.
- "Fixing alerts" is not the same as "auditing a system."

### Steal / mutate / reject

- Steal: start from grounded issue packets with exact code references.
- Mutate: instead of one alert -> one fix, let multiple agents argue over one grounded issue packet before a fix is proposed.
- Reject: the idea that the AI layer should mainly be a patch generator bolted onto a scanner.

## Finding 2: GitLab is moving from explainers to agentic remediation, but only after scanner triage

### What exists

GitLab Duo first shipped vulnerability explanation, including automatic analysis of high and critical SAST findings for likely false positives. More recently, GitLab introduced "Agentic SAST Vulnerability Resolution" as a beta feature. GitLab describes it as iterative or multi-shot reasoning that analyzes vulnerability context across the codebase, generates fixes, validates them through automated testing, and produces confidence scores. It can run automatically when a main-branch SAST scan finds a high or critical issue that is not marked likely false positive.

Sources:
- [GitLab Docs: Explain vulnerabilities with AI](https://docs.gitlab.com/user/application_security/analyze/duo/)
- [GitLab Docs: Agentic SAST Vulnerability Resolution](https://docs.gitlab.com/user/application_security/vulnerabilities/agentic_vulnerability_resolution/)
- [GitLab Docs: Remediate](https://docs.gitlab.com/user/application_security/remediate/)

### Mechanism worth noticing

- GitLab stages the work: detect -> explain / false-positive pass -> resolve with MR.
- It treats false-positive filtering as a distinct gate before auto-remediation.
- It upgrades from single-shot patching to iterative reasoning only after it already has a very specific vulnerability target.

### Strengths

- Better than pure autofix because it acknowledges that explanation, triage, and remediation are different jobs.
- Uses CI/pipeline validation as part of the remediation loop.
- Confidence scoring is a useful UX primitive even if the score itself is imperfect.

### Weaknesses

- Still downstream of SAST; recall is bounded by the analyzer.
- The "agentic" move is mostly inside remediation, not in discovery.
- Severity-driven automation may miss lower-severity-but-exploitable logic flaws and chained issues.

### Steal / mutate / reject

- Steal: separate false-positive elimination from remediation generation.
- Mutate: run the "agentic" mode in the discovery phase too, not only after scanner findings exist.
- Reject: tying the system’s imagination too tightly to severity labels emitted by upstream scanners.

## Finding 3: Semgrep Assistant is one of the clearest examples of AI as triage infrastructure

### What exists

Semgrep Assistant now spans explanation, remediation guidance, autofix, component tagging, auto-triage, noise filtering, memory/custom instructions, and weekly prioritization. It auto-analyzes many but not all new findings. For full scans it auto-analyzes new high/critical issues with sufficient confidence; for diff-aware scans it analyzes only a capped number of new findings. It can suppress PR comments when it believes a finding is a false positive. It also adapts recommendations using prior feedback and per-project instructions. Semgrep reports that its noise filtering is over 95% accurate in categorizing Semgrep Code findings as false positives, and its metrics doc says user and internal benchmarks showed large finding reduction at scale.

Sources:
- [Semgrep Docs: Overview](https://semgrep.dev/docs/semgrep-assistant/overview)
- [Semgrep Docs: Analyze findings](https://semgrep.dev/docs/semgrep-assistant/analyze)
- [Semgrep Docs: Metrics and methodology](https://semgrep.dev/docs/semgrep-assistant/metrics)

### Mechanism worth noticing

- The system learns from triage decisions, not just from training data.
- It uses AI not only to explain and patch findings, but to decide which findings should interrupt developers at all.
- It adds light organizational memory so future remediations reflect local standards.

### Strengths

- Very strong product taste around interruption management.
- Good understanding that the main bottleneck in AppSec is often trust and triage burden, not raw detection count.
- PR comments, AppSec dashboard, and feedback loops are wired together.

### Weaknesses

- Still mostly operating as an intelligence layer on top of Semgrep detections.
- Capped auto-analysis means deep attention is rationed; that is practical but reveals cost constraints.
- Per-finding assistance can still fragment a larger exploit chain into local suggestions.

### Steal / mutate / reject

- Steal: treat "should this interrupt a human?" as a first-class model output.
- Steal: project-specific memory for remediation preferences and threat context.
- Mutate: instead of only filtering individual findings, let `braind` filter entire investigative branches and hypotheses.

## Finding 4: Snyk’s hybrid-AI story is a useful architectural clue

### What exists

Snyk’s DeepCode AI and Agent Fix pitch a hybrid system rather than a single LLM. Snyk says it combines symbolic and generative AI, multiple models, security-specific training data, and program analysis. Its docs for automated code fixes say the analysis engine rigorously checks neural suggestions so the resulting fixes stay small and targeted. Snyk’s rule catalogs also explicitly mark which security rules are autofixable, which is more honest than pretending agentic fixing is universal.

Sources:
- [Snyk: DeepCode AI](https://snyk.io/platform/deepcode-ai/)
- [Snyk Docs: Fix code vulnerabilities automatically](https://docs.snyk.io/scan-with-snyk/snyk-code/manage-code-vulnerabilities/fix-code-vulnerabilities-automatically)
- [Snyk Docs: Snyk Code security rules](https://docs.snyk.io/scan-with-snyk/snyk-code/snyk-code-security-rules)

### Mechanism worth noticing

- The right architecture is not "LLM or static analysis"; it is "analysis constrains generation."
- Autofixability is a property that should be tracked explicitly per issue type.
- They appear to optimize for minimal, local, analyzable patches.

### Strengths

- Hybrid design is closer to how serious secure tooling should work.
- Clearer than many vendors about combining security research, analysis, and AI.
- Explicit autofix coverage boundaries are useful for user trust.

### Weaknesses

- The public story is still mostly vendor marketing rather than a transparent workflow description.
- Minimal patch bias can under-correct root-cause architectural issues.
- Strong analyzer dependence remains.

### Steal / mutate / reject

- Steal: store "autofixability" and "verificationability" as explicit attributes on issue packets.
- Mutate: go beyond tiny local patches when the issue is architectural, but only after proving the narrow fix is insufficient.
- Reject: black-box claims of accuracy without exposing enough artifact detail for operator trust.

## Finding 5: Checkmarx and similar vendors are racing toward IDE-resident security agents

### What exists

Checkmarx’s recent positioning centers on AI AppSec agents that live inside AI-native IDEs such as Cursor and Windsurf. The public materials emphasize real-time context-aware prevention, remediation, and guidance for AI-generated code, with a family of agents for developers, policy, and insights.

Sources:
- [Checkmarx press release: Developer Assist Agent](https://checkmarx.com/press-releases/checkmarx-enables-real-time-code-security-with-launch-of-developer-assist-agent-for-ai-native-ides/)
- [Checkmarx product page: Developer Assist](https://checkmarx.com/product/developer-assist/)
- [Checkmarx AI security page](https://checkmarx.com/solutions/ai-security/)

### Mechanism worth noticing

- The market believes security agents must show up where AI code is being written, not only in later review stages.
- There is a strong push toward "always-on copilot for secure coding" instead of "deliberate audit session."

### Strengths

- Meets developers at the moment risk is created.
- Likely lowers remediation latency dramatically.

### Weaknesses

- IDE-native guidance is prevention, not audit.
- Real-time inline agents can nudge developers toward shallow local fixes and away from system-level thinking.
- Public docs are still thin on how the agent grounds itself or verifies changes.

### Steal / mutate / reject

- Steal: eventually meet the user in the places they already write code.
- Mutate: keep the deep audit experience separate from the inline coding assistant so the product does not collapse into another chat pane.
- Reject: the idea that security review should become invisible background autocomplete.

## Cross-cutting Patterns From Current Products

### What the best implementations seem to agree on

- Detection, explanation, triage, and remediation are distinct jobs.
- AI works better when grounded in structured analyzer outputs, code locations, or explicit issue packets.
- Validation gates matter. The serious systems use tests, pipelines, or analysis checks before surfacing or accepting fixes.
- False-positive management is not a side feature. It is central product value.
- Narrow, well-scoped fixes are easier to trust than sweeping rewrites.
- Good UX means living inside an existing workflow: PRs, dashboards, IDEs, or MRs.

### What still looks missing

- Very few systems use AI to originate strong novel findings from broad repo context, then aggressively challenge those findings with adversarial agents.
- Most products reason one finding at a time. Very few appear designed to discover exploit chains or interacting weaknesses across files, services, configs, and docs.
- The user rarely gets a transparent artifact trail of how an issue survived multiple layers of skepticism.
- The strongest products optimize for remediating scanner findings, not for performing a creative security audit.

## What This Suggests For Braind

`braind` should not try to be "another scanner with an LLM on top." That market is already converging on the same shape: analyzer emits issue, model explains it, maybe filters false positives, maybe proposes a patch, CI validates. Useful, but not enough.

The opening is a system that feels more like a disciplined security investigation engine:

- Start with wide-context hunting, not only scanner alerts.
- Force every candidate issue through adversarial compression: hunter claim -> skeptic attack -> referee synthesis.
- Treat triage suppression as a first-class feature, but make the suppression auditable.
- Produce issue packets that are rich enough to support both human truth review and high-quality fixes.
- Separate "is this real?" from "how should we fix it?" so patch generation never launders a weak finding into something that merely looks plausible.
- Make exploit-chain discovery a first-class mode rather than assuming one alert equals one bug.
- Preserve the best vendor lesson: every proposed fix should be grounded, narrow when possible, and validated before being recommended.

The bold move is to combine the trustworthiness of scanner-grounded systems with the creativity of an actual audit team. That is much closer to the architecture `awdit` is already sketching than anything in the current direct market.

# Round 1 Notes: AI Security Audit Research

Date: 2026-03-27

## Repo Context

`awdit` is already pointed at a strong shape: competing hunter, skeptic, referee, and solver agents with explicit issue packets, human truth review, and artifact-heavy runs. The useful question is not whether AI can help AppSec, but which mechanisms actually improve signal, trust, and velocity without turning the product into noisy scanner theater.

## Direct Systems And What They Reveal

### GitHub Copilot Autofix

- [About Copilot Autofix for code scanning](https://docs.github.com/en/code-security/concepts/code-scanning/copilot-autofix-for-code-scanning)
- [Responsible use of Copilot Autofix for code scanning](https://docs.github.com/en/code-security/responsible-use/responsible-use-autofix-code-scanning)

Key takeaways:

- GitHub keeps the pipeline analyzer-first. CodeQL creates the alert; the LLM is asked to translate a grounded alert into a patch.
- GitHub is unusually explicit about failure modes: non-determinism, truncated context, partial fixes, semantic regressions, and even dependency hallucinations.
- The big design lesson is that the best commercial systems narrow the model's job until it becomes reliable enough to ship inside the workflow.

Raw inspiration:

- Use exact issue packets, not vague prompts.
- Track not only "fix generated" but also "fix may be semantically incomplete."
- Never let patch generation silently stand in for finding validation.

### GitHub Copilot Code Review

- [About GitHub Copilot code review](https://docs.github.com/copilot/concepts/code-review)
- [Responsible use of GitHub Copilot code review](https://docs.github.com/en/copilot/responsible-use/code-review)

Key takeaways:

- GitHub frames review as a purpose-built product with a tuned system, not an open-ended "pick any model" surface.
- It explicitly says model switching is unsupported because it would compromise reliability and UX.
- It also admits missed issues, hallucinated comments, insecure suggestions, and language/style bias.

Raw inspiration:

- Reliability may matter more than user-configurable model freedom in core review loops.
- A security-review system should optimize for stable reviewer behavior, not "bring your favorite model."
- Deduplication and focused comment presentation are features, not implementation details.

### GitLab Duo

- [Explain vulnerabilities with AI](https://docs.gitlab.com/user/application_security/analyze/duo/)
- [SAST false positive detection](https://docs.gitlab.com/user/application_security/vulnerabilities/false_positive_detection/)
- [Resolve vulnerabilities with AI](https://docs.gitlab.com/user/application_security/remediate/duo/)
- [Agentic SAST Vulnerability Resolution](https://docs.gitlab.com/user/application_security/vulnerabilities/agentic_vulnerability_resolution/)

Key takeaways:

- GitLab separates explanation, false-positive assessment, and resolution.
- False-positive detection includes a confidence score, an explanation, and a visible badge.
- Agentic remediation shows up after triage, not before it.

Raw inspiration:

- Confidence should be displayed as a review aid, not an auto-dismiss permission slip.
- "Explain," "suppress," and "fix" are different workflows and deserve different UIs.
- The strongest role for agents may begin after structured triage exists.

### Semgrep Assistant

- [Overview](https://semgrep.dev/docs/semgrep-assistant/overview)
- [Analyze findings](https://semgrep.dev/docs/semgrep-assistant/analyze)
- [Customize Assistant](https://semgrep.dev/docs/semgrep-assistant/customize)
- [Metrics and methodology](https://semgrep.dev/docs/semgrep-assistant/metrics)

Key takeaways:

- Semgrep is one of the clearest examples of AI as triage infrastructure, not just explanation.
- It auto-triages, suppresses some PR comments, adapts to project-specific instructions, tags components, and sends weekly backlog prioritization.
- Its metrics page is careful: a high false-positive confidence rate means precision when it flags noise, not recall over all possible noise.

Raw inspiration:

- "Should this interrupt anyone?" is a first-class model output.
- Feedback loops belong in the product, not in offline evaluation only.
- Confidence metrics must be labeled honestly or they train users to mistrust everything.

### Snyk DeepCode AI

- [DeepCode AI](https://snyk.io/platform/deepcode-ai/)
- [Fix code vulnerabilities automatically](https://docs.snyk.io/scan-with-snyk/snyk-code/manage-code-vulnerabilities/fix-code-vulnerabilities-automatically)

Key takeaways:

- Snyk's public story is hybrid: symbolic analysis constrains generative suggestions.
- It treats some rule classes as autofixable and others as not.

Raw inspiration:

- Autofixability should be a property on each issue packet.
- Security AI should expose coverage boundaries instead of pretending everything is equally solvable.

## Adjacent Systems Worth Stealing From

### OSS-Fuzz / ClusterFuzzLite

- [CIFuzz continuous integration](https://google.github.io/oss-fuzz/getting-started/continuous-integration/)
- [ClusterFuzzLite overview](https://google.github.io/clusterfuzzlite/)
- [ClusterFuzz testcase reports](https://google.github.io/oss-fuzz/further-reading/clusterfuzz/)

Key takeaways:

- These systems treat reproducibility as sacred.
- A crash is only worth interrupting humans over when it reproduces and appears novel.
- The UI includes stack traces, reproducer artifacts, regression ranges, coverage, and deduplicated crash reports.

Raw inspiration:

- `braind` should make every important claim reproducible when possible.
- Novelty matters; flooding a user with rediscoveries is a product failure.
- Differential evidence beats prose.

### Bug Bounty Triage Platforms

- [HackerOne duplicate reports](https://docs.hackerone.com/en/articles/8514410-duplicate-reports)
- [HackerOne duplicate detection](https://docs.hackerone.com/en/articles/8514430-duplicate-detection)
- [HackerOne report states](https://docs.hackerone.com/en/articles/8475030-report-states)
- [Bugcrowd triage](https://www.bugcrowd.com/products/platform/triage/)
- [Bugcrowd AI Triage](https://www.bugcrowd.com/blog/bugcrowd-ai-triage-speeds-vulnerability-resolution-elevates-hacker-experience/)

Key takeaways:

- Mature offensive-security workflows care obsessively about duplicate handling, report states, severity routing, and fairness.
- Duplicates are not dead data; they are linked to the canonical report and can preserve contributor credit or visibility.
- Human experts remain in the loop even when AI speeds up spam, duplicate, or criticality prediction.

Raw inspiration:

- Duplicate findings in `braind` should collapse into a canonical issue while preserving provenance.
- State transitions should be explicit and visible: candidate, challenged, referee-approved, human-confirmed, solver-ready, archived.
- Human review should feel like adjudication with receipts, not "trust the model."

## Evaluation And Feedback

### Meta AutoPatchBench

- [Introducing AutoPatchBench](https://engineering.fb.com/2025/04/29/ai-research/autopatchbench-benchmark-ai-powered-security-fixes/)

Key takeaways:

- Build success plus "crash no longer reproduces" is not enough to infer correctness.
- Meta reports that models can look decent at initial patch generation and still collapse after fuzzing and white-box differential testing.
- This is the cleanest argument I found against naive patch acceptance.

Raw inspiration:

- `braind` should separate syntax-valid, symptom-hiding, exploit-blocking, and semantically-correct fixes.
- Verification needs multiple gates with different failure meanings.

### AgenticSCR

- [AgenticSCR abstract mirror](https://papers.cool/arxiv/2601.19138)

Key takeaways:

- This early 2026 preprint argues that agentic secure code review with explicit tool use and security-focused semantic memory can substantially outperform static LLM baselines and SAST for immature, context-dependent vulnerabilities.
- The most interesting part is not the headline number. It is the design move: pair tool-using review agents with a security memory that helps them navigate context under pre-commit constraints.

Raw inspiration:

- Memory should capture security concepts and local repo context, not just previous conversation turns.
- Agentic review looks especially promising for immature logic flaws that are awkward for scanners.

## Round 1 Synthesis

Patterns that keep showing up:

- Grounded issue packets beat freeform repo spelunking once a candidate exists.
- Noise management is core product value.
- Validation needs to be deeper than build green.
- Users trust systems that expose evidence, state, and limitations.
- The best workflows do not confuse detection, adjudication, and remediation.

What still looks missing:

- First-class exploit-chain discovery across files and configs.
- Adversarial internal debate with auditable suppression.
- Honest "do not interrupt the human yet" gating for investigative branches, not just scanner findings.
- A product that feels like running a disciplined audit team rather than operating a scanner console with AI garnish.

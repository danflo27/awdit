# Round 2 Notes: Challenges To Round 1

Date: 2026-03-27

Round two was not "find more examples." It was "find what breaks our favorite ideas."

## Assumption: More Agents Automatically Means Better Review

Challenge:

- [About GitHub Copilot code review](https://docs.github.com/copilot/concepts/code-review)
- [Responsible use of GitHub Copilot code review](https://docs.github.com/en/copilot/responsible-use/code-review)

What changed:

- GitHub's strongest review product is explicitly purpose-built and tuned; it does not expose model switching because consistency matters.
- That suggests the core review path in `braind` should not be an open playground of arbitrary model combinations.

Update:

- Keep role diversity, but narrow it to roles with materially different incentives.
- Standardize models or prompt families inside a stage where consistency is more valuable than experimentation.

## Assumption: Build + Passing Tests Are Enough To Trust Fixes

Challenge:

- [Introducing AutoPatchBench](https://engineering.fb.com/2025/04/29/ai-research/autopatchbench-benchmark-ai-powered-security-fixes/)

What changed:

- Meta shows that build-plus-symptom-removal can wildly overestimate patch quality.
- Fuzzing and white-box differential testing reject many apparently successful fixes.

Update:

- `braind` should never mark a fix "good" after only compile/test success.
- Fix status should be staged: compiles, symptom removed, regression-safe, behavior-preserving, human-approved.

## Assumption: Confidence Scores Solve Trust

Challenge:

- [Semgrep metrics and methodology](https://semgrep.dev/docs/semgrep-assistant/metrics)
- [SAST false positive detection](https://docs.gitlab.com/user/application_security/vulnerabilities/false_positive_detection/)

What changed:

- Confidence is useful, but easy to misread.
- Semgrep's documentation is a useful corrective: precision on flagged noise is not the same as broad false-positive recall.

Update:

- Show confidence, but always tie it to the decision it applies to.
- Pair scores with states and explicit evidence, not with opaque finality.

## Assumption: Scanner-First Is Good Enough

Challenge:

- [AgenticSCR abstract mirror](https://papers.cool/arxiv/2601.19138)
- [ClusterFuzzLite](https://google.github.io/clusterfuzzlite/)

What changed:

- There is credible early evidence that agentic secure review can outperform scanner-first baselines on immature vulnerabilities.
- Dynamic and generative systems find different bug classes than static analyzers.

Update:

- `braind` should have two input currents:
  - scanner-grounded evidence when available
  - exploratory hunt mode for logic flaws and cross-file issues

## Assumption: More Inline Comments Means More Value

Challenge:

- [Semgrep Assistant overview](https://semgrep.dev/docs/semgrep-assistant/overview)
- [HackerOne report states](https://docs.hackerone.com/en/articles/8475030-report-states)

What changed:

- Strong products suppress interruptions and move noise into a review queue.
- Mature systems rely on explicit states more than constant commentary.

Update:

- `braind` should default to one canonical issue packet per claim, not scattered comments.
- Most debate should stay in artifacts until an issue survives enough scrutiny to deserve human time.

## Assumption: Deep Whole-Repo Analysis Should Happen On Every Run

Challenge:

- [CIFuzz continuous integration](https://google.github.io/oss-fuzz/getting-started/continuous-integration/)
- [GitHub Copilot code review](https://docs.github.com/copilot/concepts/code-review)

What changed:

- Practical systems split shallow fast-path review from slower deep-path analysis.
- Pull-request review and deeper background investigation should be different operating modes.

Update:

- `braind` should have:
  - fast diff mode for immediate review
  - deep audit mode for full investigative runs
  - background memory-building/indexing between runs

## Round 2 Bottom Line

The first proposal needed one correction more than any other:

- do not make the product "more agentic" than the user can trust.

The right move is not maximal agent count. It is disciplined stage design, explicit evidence, ruthless suppression of weak branches, and verification that is strong enough to keep flashy-but-wrong fixes from passing as wins.

# ADR 0009: Evaluation with hard gates, and input screening as a tripwire

Status: accepted

## Context
Two questions need an answer you can repeat: "is the system safe?" and "is it any good?". Averages blur them: 95% accuracy hides a single dangerous automatic fix. And prompt injection cannot be solved by detection, because any list of patterns can be paraphrased.

## Decision
- **Hard gates and soft metrics are separate.** Safety properties (no unsafe fix, no secret reaching a model, every planted injection flagged, every forbidden call refused) must hold on every scenario; one miss fails the run. Quality metrics (accuracy) are compared with a stored baseline, so they may not regress but are not demanded to be perfect.
- **Two modes.** The scripted model (default, free, in CI) tests everything around the model, including models that are overconfident, hallucinate a commit, or obey an injection. The live model measures diagnosis quality, costs money and needs credentials, so it is run by hand.
- **Evaluations must be able to fail.** Mutation tests switch off screening, the policy layer, the confidence threshold and scrubbing, and assert the evaluation reports exactly that failure.
- **Screening is a tripwire, not a wall.** It looks for instruction-like text (override, addressed-to-AI, role change, approval demands, tool names, exfiltration, forged delimiters, hidden characters) in build output and every tool result. A hit forces ticket-only through the gate and puts the reason in the ticket. It favours recall: a false positive costs a human glance, a miss is still stopped by the model having no write tools, the policy layer and the gate. Input is size-capped and all patterns bounded, so it cannot be used for denial of service.
- **A second, independent permission matrix** lives in the tests; a new MCP tool fails a test until someone classifies it.

## Consequences
- A green scripted eval proves nothing about the model's diagnostic skill. Do not report it as accuracy.
- Text beyond the scan cap is not screened (a documented limit); normal log excerpts are far smaller.
- Some benign text will be flagged ("ignore the whitespace instructions"); those runs lose automatic fixing, not their ticket.

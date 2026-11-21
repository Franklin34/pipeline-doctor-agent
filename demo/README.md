# demo

```bash
make demo                                          # three scenarios, narrated
uv run python -m pd_evals.demo logic-bug           # one scenario
uv run python -m pd_evals.demo --help
```

An offline, narrated run of the **real** pipeline: real Go Jenkins MCP server, real GitHub and Jira MCP servers over fixtures, real scrubbing, policy, screening, gate and ticket rendering. Only the language model is scripted (no key, no network, no cost), so this shows the system around the model, not the model's skill. See `evals/README.md`.

Scenarios worth running: `logic-bug` (a fixable failure), `obedient-to-injection` (a log line orders the model to push to `main`, and the model obeys; code refuses and flags it), `hallucinated-suspect` (a 97%-sure model cites a commit that is not in the range; the gate refuses). All 12 are listed in `fixtures/README.md`.

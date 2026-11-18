# Deterministic pre-processing

`agent/src/pipeline_doctor_agent/preprocess/` turns a raw Jenkins log (and optional JUnit XML) into a small, scrubbed, structured `ProcessedFailure`. It is plain code and runs **before any LLM call**; the model only ever sees its output.

```python
from pipeline_doctor_agent.preprocess import preprocess
result = preprocess(log_lines, junit_xml=None, failed_stage="Test")
```

## Pipeline

1. **Cap size.** Logs over 1M characters keep only the tail (failures are usually last) and add a warning. Lines are cut at 4,000 characters before pattern matching.
2. **Scrub secrets** (`scrub.py`). Everything after this step works on scrubbed text, so secrets cannot reach extraction, fingerprints, prompts, tickets or traces.
3. **Locate the failed stage** (given, or inferred from the first `ERROR:`/exit-code marker) and restrict extraction to it.
4. **Extract** (`logparse.py`): error lines (+1 line of context, capped at 50), Python and Java stack traces (max 5, 40 lines each), error type and message, innermost project frame, failing test ids.
5. **Parse JUnit** (`junit.py`) with `defusedxml`; fields are scrubbed after parsing so the markup stays valid.
6. **Fingerprint** (`fingerprint.py`).

## Secret scrubbing

Redacted kinds: private keys, AWS access keys, GitHub tokens, Slack tokens, Google API keys, Stripe-style keys, JWTs, `Authorization`/`Bearer` values, credentials in URLs (`scheme://[REDACTED]@host` keeps host for diagnosis), and any `name=value` / `"name": "value"` whose name contains password, secret, token, api key, access/account/private key, credential and similar.

- Replacement is `[REDACTED:<kind>]`; `redactions` reports counts per kind, never values.
- **Idempotent** and **biased to over-redact**: a false positive costs a bit of context, a miss leaks a credential.
- **Known limit:** a bare high-entropy string with no label and no known prefix is not detected. Mitigations: least-privilege credentials everywhere, and the same scrubber applied again to outgoing tickets and PRs (Phases 6-7).

## Failure fingerprint

`sha256("v1" | error type | location)[:16]` (16 hex characters, the format the Jira tools accept).

- **Error type:** Python traceback type, pytest `file:line: Type`, `E  Type:` lines, `FAILED ... - Type`, or a Java exception.
- **Location:** innermost project stack frame (`app/service.py:load_config`, repo-relative, no line number), else the lowest failing test id, else the JUnit failure, else `stage:<name>`.
- Run-specific noise (numbers, IPs, UUIDs, timestamps, temp paths, hex ids) is normalized away. If no error type is known, the normalized first error line is used so unrelated failures do not collapse into one fingerprint.
- Changing the algorithm means bumping `VERSION`, which intentionally changes every fingerprint.

The infra fixture's open Jira issue (`DEMO-7`) carries the fingerprint this code computes for it; a test enforces that, so dedupe can be exercised end to end later.

## Hostile input

Logs are attacker-controlled (anyone who can push a commit influences them). A review found that two patterns were quadratic on inputs like `a.a.a.a.…`: an unbounded prefix in the generic secret pattern, and an unbounded exception-type pattern. Fixes: no prefix capture (only the value is replaced), bounded quantifiers everywhere, size and line caps. `tests/preprocess/test_hostile_input.py` times 13 adversarial inputs so a regression fails CI. XML uses `defusedxml` (entity expansion and external entities are rejected).

## Not done here

- Prompt-injection **flagging** is a separate guardrail hook (Phase 9). Injected text in a log stays untrusted data; it cannot change the fingerprint beyond choosing an error type string.
- Python's `re` has no match timeout. Bounded patterns plus size caps are the defence; running pre-processing in a worker with a wall-clock limit (Phase 4 run limits) is the backstop.

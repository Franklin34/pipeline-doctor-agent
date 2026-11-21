# Verification status

What has actually been run, against what, and what has not. Written so a reader can decide how much to trust each claim. "Offline" means no external service: a scripted model, fixture data, in-memory or local containers.

## How things are tested

| Layer | Method |
|---|---|
| Unit and component | pytest (strict mypy, ruff) for Python; `go test -race` for Go |
| MCP servers | in-memory MCP clients, plus the real Go server over stdio from the agent tests |
| Retrieval | both the in-memory store and **real Postgres with pgvector** (`make test-pg`, a throwaway container) |
| Sandbox | **real Docker**: network and DNS blocked, non-root, no capabilities, read-only filesystem, no host secrets, memory kill, process limit, timeout kill, no leaked containers |
| Fix flow | queue to diagnosis to **real-Docker-verified fix** to draft PR to ticket with PR link, with only the model scripted |
| Queue | Redis Streams with fakeredis; **Pub/Sub emulator** in Docker (delivery, redelivery, dead-letter, lease extension against a competing worker) |
| Safety | permission matrix over every agent and tool; 25 attack and 15 benign phrasings for screening; timing tests on hostile input |
| Evals | 12 scenarios, hard safety gates and a quality baseline; **mutation tests** that break the system on purpose and require the evaluation to notice |
| Observability | metrics through the real flow; trace continuation from the ingress's `traceparent`; a control test proving content would leak if the switch were on |

## Against real external services

| Service | What was done | Result |
|---|---|---|
| GitHub (one private repo) | read-only calls; one branch, one commit to an existing file, one draft PR through the full MCP stack; a second PR request; refusals (default branch, other repo, protected paths, new file); tarball download | all behaved as designed; default branch untouched; PR #1 left for the owner to close |
| Pub/Sub | the official emulator, not Google Cloud | passes |
| Docker | the real daemon | passes |
| Postgres + pgvector | a real container | passes |
| Terraform | `terraform validate` with the real provider schema | passes |

## Not verified

| Item | Why it matters |
|---|---|
| **A real language model** (Gemini) | **One scenario** (`logic-bug`) has been run live on `gemini-3.5-flash`: correct category, suspect and gate decision (17 steps, ~50k tokens; the policy layer refused 2 tool calls the model attempted, which were not inspected). The other 11 were **not run**: the free-tier key allows 20 requests per day per model. Diagnosis quality is therefore **essentially unmeasured**. The evals prove the system around the model, including a model that is wrong, overconfident or obeys an injection; they say nothing about how good a real model is. Run `make eval-live` on a key with billing enabled to measure it. The live run also found two bugs no scripted test could: stale default model names, and a response schema Gemini rejects (`additionalProperties`) |
| **Real Jira** | The backend and the ADF conversion are tested against a fake built from documented shapes; required custom fields, workflows and real permissions are unknown |
| **Real Jenkins** | There is no backend. Everything about builds comes from fixtures |
| **Google Cloud** | The Terraform has never run `plan` or `apply`; the first apply will probably surface issues. Fixes cannot run there (no Docker) |
| **GitHub Actions** | The workflow has never run on GitHub (it did have one real bug found by pinning: a non-existent action tag) |
| **Gemini embeddings** | Retrieval was tested with an offline hash embedder, which is not semantic |
| **Scale** | Nothing was load-tested; Pub/Sub, Redis and the model API limits are unexercised |
| **Pre-commit hooks (gitleaks)** | Configured, not installed locally, never run |
| **Full failure-to-PR on real GitHub** | Pieces were verified separately; the whole path was not run |

## Known gaps in the design (not bugs)
- Screening is heuristic: paraphrase, other languages and encodings can evade it. The safety does not depend on it.
- A wrong but confident, in-range diagnosis can still lead to a draft PR. Tests and review are the backstop.
- The install step of the sandbox has network access (wheels only, non-root).
- Cloud Run cannot run the sandbox; fixes there need a different runner.
- Redis traffic in the cloud deployment is unencrypted inside the VPC.
- No rate limiting in front of the public ingress.

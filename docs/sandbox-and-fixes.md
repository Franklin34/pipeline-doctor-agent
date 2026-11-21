# Sandbox and automatic fixes

When the diagnosis says "code, test or dependency, confident, and a commit is to blame", Pipeline Doctor may try to fix it. **Off by default** (`FIX_ENABLED=true` to enable). The result is never a merge: at most a **draft pull request** from a `pipeline-doctor/` branch, which a human reviews.

## The flow (`agent/.../fix/service.py`)

```
gate allows a fix -> checkout the failing commit -> install dependencies (cached)
 -> REPRODUCE: run the tests unchanged in the sandbox; they must fail          (else: skipped, not fixed)
 -> up to 3 attempts:
      model returns edits (path, old_text, new_text); it has no tools
      patch rules check them (deterministic)                                   (violation: feedback, next attempt)
      apply to a throw-away copy; run the tests in the sandbox
      judge: previously failing tests pass, nothing else fails, no test removed (else: feedback, next attempt)
 -> pass: create branch, commit the changed files, open a DRAFT PR             (fail x3: ticket only)
```

Every ending is a `FixOutcome` (`pr_opened`, `reused_pr`, `skipped`, `failed`, `error`) that the ticket reports. The fix is best effort: whatever happens, the ticket is still written.

## The sandbox (`sandbox/`)

Two containers per check, both removed afterwards:

| | install | test |
|---|---|---|
| Network | on (pip must reach the index) | **none** |
| Mounted | only the requirements file | repository **read-only**, dependencies **read-only**, one results directory |
| Root filesystem | read-only, `/tmp` tmpfs | read-only, `/tmp` tmpfs |
| User / privileges | `65534:65534`, `--cap-drop ALL`, `no-new-privileges` | same |
| Limits | memory, CPU, pids, ulimits, timeout | same |
| Special | `pip --only-binary=:all:` (no sdist `setup.py` ever runs) | host environment never passed; no Docker socket |

Dependencies are installed into a directory cached by `sha256(image + requirements)`; failed installs are not cached. A timeout kills the **container** (not just the `docker` client), cancellation removes it, and `cleanup_orphans()` sweeps labelled leftovers. Output is capped; the JUnit file the tests write is parsed with `defusedxml`.

`build_test_args` / `build_install_args` are pure functions, and `sandbox/tests/test_args.py` asserts every flag (and the absence of `--privileged`, `--cap-add`, host namespaces, `-v`, the Docker socket, `--env-file`...). `sandbox/tests/test_docker_real.py` proves the claims against real Docker: it tries to reach the network (including DNS), write to `/etc`, `/work` and `/deps`, read host secrets, exceed memory, fork-bomb, and run forever, and asserts each fails and no container is left.

## What a patch may and may not do

The model proposes **edits**, not a diff or whole files: `old_text` must occur exactly once in an existing file. That keeps changes minimal, makes them mechanically checkable, and gives precise retry feedback ("old_text not found", "appears 2 times").

`fix/patch_policy.py` rejects, with a reason fed back to the model (it still costs an attempt):

- more than 5 edits or 200 changed lines; unsafe paths; files that do not exist (no creating or deleting files);
- **protected paths**: CI and pipeline definitions, Dockerfiles, infrastructure, `pyproject`/`setup`/`tox`/`pytest.ini`/`conftest.py`, Makefile, lock files, CODEOWNERS, anything that looks like a credential;
- editing tests for a `code` or `dependency` failure (and editing code for a `test` failure);
- dependency files unless the failure is a `dependency` one, and then only version changes: **the set of package names may not change**;
- added code that skips or neuters tests, runs commands, uses the network, `eval`/`exec`, pickle or ctypes (a heuristic: a false positive costs an attempt and ends in a ticket).

Then the **judge** (`pd_sandbox.judge`) decides: the tests exited cleanly, none fail, none disappeared (deleting the failing test is not a fix), and every previously failing test passes. **Zero tests is never a pass.**

## Where the rules are enforced (defence in depth)

| Layer | Enforces |
|---|---|
| Prompt | minimal change, no tests/config/network (advisory only) |
| `patch_policy` | the list above, before anything runs |
| Sandbox | no network, no secrets, read-only, limits |
| Judge | real fix, no removed tests |
| `pr_publisher` tool policy | only this code identity holds the git write tools; pinned to the failing repo |
| GitHub MCP server | branch must start with `pipeline-doctor/`, never the default branch, protected paths refused again, draft only, PR only into the default branch, no PR without a commit |
| GitHub (your setup) | branch protection on the default branch, required reviews |
| A human | reviews and merges |

The fix LLM has **no tools at all**. Everything it reads from the repository or the build is fenced with a random per-request boundary id (text inside a file cannot fake the end of its block), scrubbed first, and labelled untrusted.

## One fix per failure

- `fix:<event>`: an event has one outcome; a redelivery reuses it, so no second PR.
- `fixpr:<fingerprint>`: a PR for this failure exists; later builds link it (`reused_pr`).
- `fixclaim:<fingerprint>`: one fix attempt at a time per failure; it outlives a failed attempt for an hour, so the same failure is not retried with the model over and over. The event that took the claim can resume it after a crash.
- The queue lease and the reclaim timeout are set longer than a diagnosis plus a fix, so a second worker never picks up the same event.
- Publishing is idempotent: an existing branch is reused and the PR tool returns the already-open PR for the same branch.

## Running it

```sh
docker pull python:3.13-slim          # once
FIX_ENABLED=true make up              # (mock mode: local repositories in fixtures/repos)
make test                             # includes real-Docker tests when Docker and the image exist
PD_SKIP_DOCKER_TESTS=1 make test      # skip them
```

The Docker tests download one wheel (`pytest`) from PyPI inside the install container.

## Known limits

- **Where the code comes from.** Mock mode copies `fixtures/repos/<owner>__<name>`; real mode downloads the tarball at the failing commit through the GitHub API (`GitHubTarballSource`: no git binary, no credentials on disk, hostile-archive checks, size caps). The real path has been exercised read-only against one repository; a full failure-to-PR run on real GitHub has not been done.
- **Needs Docker.** The cloud (Cloud Run) deployment cannot run the sandbox, so fixes are off there.
- **Python and pytest only.** The sandbox command and the JUnit parsing are pytest's; other ecosystems need their own runner.
- **The install container has network access.** It runs only `pip install --only-binary` of the repository's own requirements (a fix may change versions but never package names); it is non-root, capability-free and limited, but a malicious wheel from the index could still run during unpacking-time hooks. Pin and hash requirements in the target repository for stronger protection.
- The suspicious-code scan is heuristic. The real guarantees are the sandbox, the judge, the server-side path rules and the human review.
- Docker on macOS and Linux behave the same for these flags, but Docker Desktop maps file ownership loosely, so permission-related bugs would show up first in CI on Linux.

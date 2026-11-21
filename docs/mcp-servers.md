# MCP servers

Three servers give the agents their tools. Each runs over stdio, has a **mock mode** backed by `fixtures/`, and is built so the model can only do what code allows.

| Server | Language | Tools | Writes |
|---|---|---|---|
| Jenkins (`mcp-servers/jenkins`) | Go | `get_build`, `get_failed_log_excerpt`, `get_test_report`, `get_last_successful_build` | none. `rebuild` exists only if `JENKINS_REBUILD_ENABLED=true` |
| GitHub (`mcp-servers/github`) | Python | `list_commits_between`, `get_commit_diff`, `get_file`, `get_codeowners` | `create_branch`, `open_draft_pull_request`, both restricted |
| Jira (`mcp-servers/jira`) | Python | `search_similar_issues` | `create_issue`, `add_comment` (one project, set by config) |

## Design rules applied to every tool

1. **Compact outputs.** Only the fields the agent needs. The log tool returns error lines from the failed stage (default 50, max 200 lines), never the full log. Diffs, files and messages are truncated and flagged `truncated`.
2. **Strict input validation, in the schema.** Patterns and length limits live in the JSON Schema (the model sees them) and are enforced before handler code runs. Paths reject `..`, absolute paths and backslashes.
3. **Errors the model can act on.** `not found ... use SHAs from get_build`, `refused: ... use add_comment on DEMO-7 instead`. Errors are tool results (`isError`), not protocol failures, so the model can self-correct.
4. **Untrusted content is labelled.** Tools returning logs, commits, diffs, files or ticket text say so in their description. This is defence in depth: the real defence is that no tool can do harm (rule 5) and prompts delimit this content (Phase 4).
5. **Writes are restricted in code, not by prompting.**
   - Branches must start with `pipeline-doctor/`; unsafe names (`..`, `//`, `.lock`) are refused.
   - Writing to the default branch is refused explicitly.
   - Pull requests are always drafts, must come from a `pipeline-doctor/` branch and may only target the default branch. A footer is appended by code.
   - Jira `create_issue` refuses when an open issue with the same fingerprint exists (prevents duplicate tickets); there is no project argument.
6. **Read-only annotations** (`readOnlyHint`) mark read tools so clients and the Phase 4 policy layer can tell them apart.
7. **Backend interface.** Tools depend on a backend (Go interface / Python Protocol). Mock mode is the fixture backend; real GitHub and Jira backends sit behind the same interface (below); Jenkins is still fixtures-only.

Writes in mock mode only change in-memory state.

## Run and test

```sh
make test test-go          # all tests, in-process, no network
```

Run a server over stdio (stdout is the protocol; logs go to stderr):

```sh
MCP_MODE=mock FIXTURES_DIR=fixtures go run ./mcp-servers/jenkins/cmd/jenkins-mcp
MCP_MODE=mock FIXTURES_DIR=fixtures uv run python -m pd_mcp_github
MCP_MODE=mock FIXTURES_DIR=fixtures uv run python -m pd_mcp_jira
```

### Try them with the MCP Inspector

The Inspector is an interactive UI for calling tools by hand. It runs through `npx` (downloads the package on first use), from the repo root:

```sh
npx @modelcontextprotocol/inspector -e MCP_MODE=mock -e FIXTURES_DIR=fixtures \
  uv run python -m pd_mcp_github
```

Then open the printed URL, **Connect**, **Tools -> List Tools**, and try:

- `list_commits_between` with `repo=acme/demo-service`, `base_sha=a1b2c3d4e5f60718293a4b5c6d7e8f9012345678`, `head_sha=c3d4e5f60718293a4b5c6d7e8f90123456789012`
- `create_branch` with `name=main` (refused) and `name=pipeline-doctor/fix-1` (allowed)
- Jira: `search_similar_issues` with `fingerprint=fdacb5a1cf4e5ebe`, then `create_issue` with the same fingerprint (refused as duplicate)

For the Go server use `-- go run ./mcp-servers/jenkins/cmd/jenkins-mcp` with the same `-e` flags; try `get_failed_log_excerpt` with `job=demo-service/main`, `build=12`.

## Known limitations

- When `max_lines` is smaller than the number of error lines, the head+tail truncation can drop useful middle lines. Phase 3 pre-processing selects the key lines deterministically before any LLM sees them.
- Real Jenkins backend: **not built** (no Jenkins to test against). GitHub and Jira backends exist; see below.
- Scrubbing happens in the agent and again in the Jenkins server's excerpt step; see docs/preprocess.md.


## Real GitHub mode (Phase 10)

`GITHUB_MODE=real` (or `MCP_MODE=real`) switches the GitHub server and the fix flow's repository source from fixtures to GitHub. Jenkins and Jira stay on fixtures until their real backends exist.

| Concern | Rule |
|---|---|
| Credentials | One fine-grained token (Contents RW, Pull requests RW, Metadata R) limited to the test repositories. It is passed **only** to the GitHub MCP subprocess, never to Jenkins or Jira, and is excluded from `Settings.__repr__`. |
| Reach | `GITHUB_ALLOWED_REPOS` is mandatory; any other repository is refused before a request is made. |
| Writes | Branch names, protected paths and draft-only are enforced by the tool layer *and* the backend. `commit_files` only updates files that already exist. A PR is a draft or it does not exist; there is no fallback. Re-opening a PR for the same branch returns the existing one. |
| Source for fixes | `GitHubTarballSource` downloads the tarball at the failing commit (no `git`, no hooks, no credentials on disk), forwards no credentials to the redirect host, extracts regular files only, and caps size and count. |
| Errors | Status classes with short messages; never the token or headers. Reads retry on 502/503/504; writes never retry. |

**Verified against a fake** of the REST API (documented response shapes) and, on 2026-11-21, **against real GitHub** on one private test repository: reads (default branch, commit range, diffs, files), then one write round trip through the full MCP stack (branch, one commit to an existing file, a draft PR; a second request returned the same PR; the default branch was untouched), and live refusals (default branch, other repository, protected paths, new files). **Not verified:** rate-limit behaviour under load, GitHub Enterprise Server, repositories on plans that refuse draft PRs, executable file modes (`commit_files` writes mode 100644), the tarball download path (`GitHubTarballSource`) and the whole queue-to-PR flow against real GitHub.


## Real Jira mode (Phase 10)

`JIRA_MODE=real` (or `MCP_MODE=real`) switches the Jira server to Jira Cloud's REST API v3. **Not yet run against a real Jira site.**

| Concern | Rule |
|---|---|
| Credentials | Basic auth with a service account's email and API token, passed only to the Jira subprocess, excluded from `Settings.__repr__`. The account should have access to one project. |
| One project | `JIRA_PROJECT_KEY` is configuration. Searches are constrained to it in JQL, issues are created in it, and comments refuse any other project's keys. |
| Fingerprint | Stored as the label `pd-fp-<fingerprint>` (searchable, no custom field, visible to humans). The search validates the fingerprint as hex, so JQL cannot be injected. |
| Format | Descriptions and comments are converted from our wiki-markup subset to **ADF** by building nodes, never by pasting text into markup. Log text containing `{code}`, `[~mention]`, `!image!`, HTML or a `javascript:` link stays inert literal text. |
| Assignment | CODEOWNERS handles map to Jira account ids through `JIRA_USER_MAP_FILE`. An unmapped or unassignable owner leaves the ticket unassigned; the owners are still listed in the body. |
| Idempotency | The comment marker is looked up in the issue's last 100 comments before posting. |
| Retries | Reads (including Jira's read-only POST search) retry on 502/503/504; writes never retry. |

Jira Cloud's search endpoint changed in 2025 (`POST /rest/api/3/search/jql`, token pagination); that is the one used. Workflow transitions, custom issue types and required custom fields are **not** handled: if your project requires extra fields on create, the call fails with Jira's message.

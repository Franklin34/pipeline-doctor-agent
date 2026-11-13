# Architecture

```mermaid
flowchart LR
  J[Jenkins] -- webhook --> I["Ingress (Go)"]
  I -- validate + dedupe --> Q[(Queue)]
  Q -. max attempts .-> DLQ[(Dead-letter queue)]
  Q --> O
  subgraph O["Orchestrator (Python + ADK)"]
    direction TB
    P["ParallelAgent: log_analyst, code_investigator, knowledge_agent"]
    P --> D[diagnose_agent]
    D --> G{"Policy gate (plain code)"}
    G -- "fixable + confident" --> F["fix_agent (sandbox loop)"]
    G -- otherwise --> T[ticket_agent]
    F --> T
  end
  O --> MJ["Jenkins MCP (Go)"]
  O --> MG["GitHub MCP (Python)"]
  O --> MI["Jira MCP (Python)"]
  O --> R[("RAG: Postgres + pgvector")]
```

Principles: deterministic orchestration with LLM reasoning inside steps; the model proposes, code decides; all external content is untrusted; humans approve every code change.

Component docs are added as each phase lands.

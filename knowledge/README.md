# knowledge

Markdown the RAG layer indexes. `runbooks/` holds procedures, `incidents/` holds past incidents.

Optional front matter: `title:` and `date:` (YYYY-MM-DD). Sections are split by headings (`#` to `####`), so use descriptive headings. Mention the failure **fingerprint** in incident notes so exact-token search can find them.

Ingest: `uv run python -m pipeline_doctor_agent.knowledge ingest` (needs `DATABASE_URL`). Treat this directory as trusted-but-reviewed: its content is shown to the model as untrusted data, and a poisoned runbook is a real attack path, so changes go through code review.

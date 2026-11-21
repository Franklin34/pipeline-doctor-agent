---
title: Orders integration tests could not reach Postgres
date: 2026-09-14
---
# Orders integration tests could not reach Postgres

Fingerprint: fdacb5a1cf4e5ebe
Category: infrastructure
Ticket: DEMO-7

## Summary
The Integration Test stage failed on every build for about two hours with `psycopg2.OperationalError ... Connection refused`. The commits in that window changed only documentation.

## Root cause
The Postgres service container on one Jenkins agent did not start after an agent image update. Application code was fine.

## Resolution
The agent image was rolled back and the stage was re-run. No code change was needed. Opening a code pull request would have been wrong.

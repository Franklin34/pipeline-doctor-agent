---
title: Postgres connection refused in CI
date: 2026-08-20
---
# Postgres connection refused in CI

Symptoms: integration tests fail with `psycopg2.OperationalError: connection to server at "postgres" port 5432 failed: Connection refused`. Unit tests pass. The failing commit usually does not touch database code.

## Diagnosis
This is an infrastructure problem, not a code change. Confirm that the failing commit only touches docs or unrelated files. Check whether the `postgres` service container started in the pipeline's Docker network and whether the previous green build ran on a different agent.

## Fix
1. Check the health of the Postgres service in the Jenkins agent pool or the compose stack used by the Integration Test stage.
2. Re-run the stage once the service is healthy. Do not change application code.
3. If the service is healthy but unreachable, check the network alias and the `DATABASE_URL` used by the stage.

## What not to do
Do not add retries or sleeps to the tests, and do not open a code pull request for this failure. Create or update an infrastructure ticket instead.

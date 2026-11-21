---
title: Dependency upgrade breaks the build
date: 2026-06-11
---
# Dependency upgrade breaks the build

Symptoms: `ImportError`, `AttributeError` or `TypeError` in code that did not change, after a commit that edits `requirements.txt`, `pyproject.toml` or a lock file.

## Diagnosis
Look for a changed version pin or a transitive upgrade in the lock file. Read the dependency's changelog for removed or renamed APIs.

## Fix
1. Pin the dependency back to the last working version as the quickest safe change.
2. Or adapt the calling code to the new API in the same pull request, with tests.
3. Never remove a failing test to make the build green.

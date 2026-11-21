---
title: Pytest assertion failure after a refactor
date: 2026-07-02
---
# Pytest assertion failure after a refactor

Symptoms: a unit test that passed on the last green build now fails with `AssertionError`. The failing test exercises a function that was modified in the commits since the last green build.

## Diagnosis
Compare the commits between the last green build and the failing build. A refactor that changes arithmetic, comparison direction, or default values is the most common cause. Read the diff of the changed function next to the failing assertion: the expected and actual values usually show whether a formula was inverted.

## Fix
1. Identify the single commit that changed the function under test.
2. Restore the intended behaviour in the function; keep the test unchanged unless the test itself encoded wrong behaviour.
3. Run the full unit test suite before opening a draft pull request.

## Confidence guidance
High confidence when exactly one commit touches the failing function and the assertion values match the changed expression. Low confidence when several commits touch it.

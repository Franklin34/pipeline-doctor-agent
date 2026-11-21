---
title: Handling flaky tests
date: 2026-05-03
---
# Handling flaky tests

Symptoms: a test fails intermittently with no relevant change between the last green and the failing build; re-running the build passes.

## Diagnosis
A failure is likely flaky when the failing commit does not touch the code under test, the same test passed on neighbouring builds, and the error involves timing, ordering, network or shared state.

## Recommendation
1. Do not change code in response to one flaky failure.
2. Record the test name and the failure in the flaky-test ticket.
3. Quarantine the test only through the team's review process.

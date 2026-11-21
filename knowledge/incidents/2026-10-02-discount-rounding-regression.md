---
title: Discount calculation regression after refactor
date: 2026-10-02
---
# Discount calculation regression after refactor

Category: code

## Summary
`test_apply_discount` failed with `assert 10.0 == 90.0` after a refactor of the pricing module.

## Root cause
The refactor replaced `price * (1 - rate)` with `price * rate`, so the function returned the discount amount instead of the discounted price.

## Resolution
The expression was restored and a second test with a different rate was added. The fix was a one-line change reviewed in a draft pull request.
